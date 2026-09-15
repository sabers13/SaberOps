"""Small, non-secret contracts for tool transport.

This module deliberately contains no provider, model, network, or orchestration
logic.  A manifest is configuration evidence; it is not a workflow engine.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, cast


class ToolManifestError(ValueError):
    """Raised when a tool manifest or logical reference is unsafe or invalid."""


class ToolTransport(StrEnum):
    """Transport of a logical tool, distinct from provider transport."""

    NATIVE_CLI = "NATIVE_CLI"
    MCP_TO_CLI = "MCP_TO_CLI"
    NATIVE_MCP = "NATIVE_MCP"


class VerificationState(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    STALE = "STALE"
    FAILED = "FAILED"


class SideEffectClass(StrEnum):
    READ_ONLY = "READ_ONLY"
    MUTATING = "MUTATING"
    UNKNOWN = "UNKNOWN"


_REF_RE = re.compile(r"^[a-z][a-z0-9_-]*\.[a-z][a-z0-9_-]*$")
_BUNDLE_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEY_RE = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|authorization|bearer|access[_-]?token|refresh[_-]?token)$",
    re.IGNORECASE,
)
_ALLOWED_AUTH_PREFIXES = (
    "env:",
    "profile:",
    "store:",
    "environment-variable:",
    "credential-profile:",
    "credential-store:",
)


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _reject_secrets(value: object, key: str = "") -> None:
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            key_text = str(child_key)
            if _SECRET_KEY_RE.search(key_text):
                raise ToolManifestError(
                    f"secret-bearing manifest field is not permitted: {key_text}"
                )
            _reject_secrets(child_value, key_text)
    elif isinstance(value, list):
        for child in value:
            _reject_secrets(child, key)
    elif isinstance(value, str):
        lowered = value.lower()
        if (
            "bearer " in lowered
            or "-----begin " in lowered
            or re.search(r"(?:token|api[_-]?key|password)=", lowered) is not None
        ):
            raise ToolManifestError("obvious secret material is not permitted in a manifest")


def _safe_path(value: str | None) -> str | None:
    if value is None:
        return None
    path = Path(value)
    if any(part == ".." for part in path.parts):
        raise ToolManifestError("path traversal is not permitted in artifact paths")
    return str(path)


def validate_logical_ref(value: str) -> str:
    """Validate and normalize the persisted ``bundle.tool`` reference."""
    ref = str(value).strip()
    if not _REF_RE.fullmatch(ref):
        raise ToolManifestError("logical tool ref must match lowercase bundle.tool format")
    return ref


@dataclass(frozen=True)
class LogicalToolRef:
    """Stable semantic identity; it never contains an executable path."""

    value: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "value", validate_logical_ref(self.value))

    @property
    def bundle_id(self) -> str:
        return self.value.split(".", 1)[0]

    @property
    def tool_name(self) -> str:
        return self.value.split(".", 1)[1]

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ToolSpec:
    ref: str
    hint: str
    command: str | None = None
    side_effect: SideEffectClass = SideEffectClass.UNKNOWN

    def __post_init__(self) -> None:
        object.__setattr__(self, "ref", validate_logical_ref(self.ref))
        if not self.hint.strip() or len(self.hint.encode("utf-8")) > 512:
            raise ToolManifestError("tool hint must be non-empty and bounded")
        if self.command is not None and (
            not self.command or any(part == ".." for part in Path(self.command).parts)
        ):
            raise ToolManifestError("invalid logical tool command")

    def as_dict(self) -> dict[str, object]:
        return {
            "ref": self.ref,
            "hint": self.hint,
            "command": self.command,
            "side_effect": self.side_effect.value,
        }


@dataclass(frozen=True)
class ToolBundleManifest:
    """Canonical non-secret bundle description."""

    schema_version: int
    bundle_id: str
    display_name: str
    transport: ToolTransport
    source_kind: str
    source_descriptor: dict[str, object]
    tools: tuple[ToolSpec, ...]
    artifact_path: str | None = None
    include_tools: tuple[str, ...] = ()
    exclude_tools: tuple[str, ...] = ()
    generator_name: str | None = None
    generator_version: str | None = None
    generation_config_digest: str | None = None
    artifact_sha256: str | None = None
    auth_reference: str | None = None
    verification_state: VerificationState = VerificationState.UNVERIFIED
    verification_timestamp: str | None = None
    manifest_digest: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ToolManifestError("unsupported tool manifest schema_version")
        if not _BUNDLE_RE.fullmatch(self.bundle_id):
            raise ToolManifestError("bundle_id must be lowercase and path-safe")
        if self.transport == ToolTransport.MCP_TO_CLI and self.source_kind not in (
            "http_mcp",
            "stdio_mcp",
        ):
            raise ToolManifestError("MCP_TO_CLI requires http_mcp or stdio_mcp source_kind")
        if self.transport == ToolTransport.NATIVE_CLI and self.source_kind != "native_cli":
            raise ToolManifestError("NATIVE_CLI requires native_cli source_kind")
        if self.transport == ToolTransport.NATIVE_MCP and self.source_kind not in (
            "http_mcp",
            "stdio_mcp",
            "native_mcp",
        ):
            raise ToolManifestError("NATIVE_MCP requires an MCP source kind")
        refs = [spec.ref for spec in self.tools]
        if len(refs) != len(set(refs)):
            raise ToolManifestError("duplicate logical tool refs are not permitted")
        for name in (*self.include_tools, *self.exclude_tools):
            validate_logical_ref(f"{self.bundle_id}.{name}" if "." not in name else name)
        if self.auth_reference is not None and not self.auth_reference.startswith(
            _ALLOWED_AUTH_PREFIXES
        ):
            raise ToolManifestError("auth_reference must name an env, profile, or external store")
        _safe_path(self.artifact_path)
        _reject_secrets(self.source_descriptor)

    @property
    def canonical_digest(self) -> str:
        return _digest(self.as_dict(include_digest=False))

    @property
    def canonical_manifest_digest(self) -> str:
        return self.canonical_digest

    @property
    def logical_tools(self) -> tuple[ToolSpec, ...]:
        return self.tools

    @property
    def generated_artifact_location(self) -> str | None:
        return self.artifact_path

    def as_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        data: dict[str, object] = {
            "schema_version": self.schema_version,
            "bundle_id": self.bundle_id,
            "display_name": self.display_name,
            "transport": self.transport.value,
            "source_kind": self.source_kind,
            "source_descriptor": self.source_descriptor,
            "artifact_path": self.artifact_path,
            "logical_tools": [
                spec.as_dict() for spec in sorted(self.tools, key=lambda item: item.ref)
            ],
            "include_tools": sorted(self.include_tools),
            "exclude_tools": sorted(self.exclude_tools),
            "generator_name": self.generator_name,
            "generator_version": self.generator_version,
            "generation_config_digest": self.generation_config_digest,
            "generated_artifact_sha256": self.artifact_sha256,
            "auth_reference": self.auth_reference,
            "verification_state": self.verification_state.value,
            "verification_timestamp": self.verification_timestamp,
        }
        if include_digest:
            data["manifest_digest"] = self.manifest_digest or self.canonical_digest
        return data

    def to_json(self) -> str:
        return _canonical(self.as_dict())

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> ToolBundleManifest:
        if not isinstance(raw, dict):
            raise ToolManifestError("manifest must be a JSON object")
        _reject_secrets(raw)
        try:
            raw_tools = raw.get("logical_tools", [])
            raw_include = raw.get("include_tools", [])
            raw_exclude = raw.get("exclude_tools", [])
            raw_source = raw.get("source_descriptor", {})
            if not isinstance(raw_tools, list) or any(
                not isinstance(item, dict) for item in raw_tools
            ):
                raise ToolManifestError("logical_tools must be a list of objects")
            if not isinstance(raw_include, list) or any(
                not isinstance(item, str) for item in raw_include
            ):
                raise ToolManifestError("include_tools must be a list of strings")
            if not isinstance(raw_exclude, list) or any(
                not isinstance(item, str) for item in raw_exclude
            ):
                raise ToolManifestError("exclude_tools must be a list of strings")
            if not isinstance(raw_source, dict):
                raise ToolManifestError("source_descriptor must be an object")
            for item in raw_tools:
                if not isinstance(item.get("ref"), str) or not isinstance(
                    item.get("hint"), str
                ):
                    raise ToolManifestError("each logical tool requires string ref and hint")
                if item.get("command") is not None and not isinstance(item.get("command"), str):
                    raise ToolManifestError("logical tool command must be a string")
                if not isinstance(item.get("side_effect", "UNKNOWN"), str):
                    raise ToolManifestError("logical tool side_effect must be a string")
            specs = tuple(
                ToolSpec(
                    ref=cast(str, item["ref"]),
                    hint=cast(str, item["hint"]),
                    command=cast(str, item["command"])
                    if item.get("command") is not None
                    else None,
                    side_effect=SideEffectClass(cast(str, item.get("side_effect", "UNKNOWN"))),
                )
                for item in raw_tools
            )
            manifest = cls(
                schema_version=int(cast(str | int, raw["schema_version"])),
                bundle_id=str(raw["bundle_id"]),
                display_name=str(raw.get("display_name", raw["bundle_id"])),
                transport=ToolTransport(str(raw["transport"])),
                source_kind=str(raw["source_kind"]),
                source_descriptor=raw_source,
                tools=specs,
                artifact_path=str(raw["artifact_path"]) if raw.get("artifact_path") else None,
                include_tools=tuple(raw_include),
                exclude_tools=tuple(raw_exclude),
                generator_name=str(raw["generator_name"]) if raw.get("generator_name") else None,
                generator_version=str(raw["generator_version"])
                if raw.get("generator_version")
                else None,
                generation_config_digest=str(raw["generation_config_digest"])
                if raw.get("generation_config_digest")
                else None,
                artifact_sha256=str(raw["generated_artifact_sha256"])
                if raw.get("generated_artifact_sha256")
                else None,
                auth_reference=str(raw["auth_reference"]) if raw.get("auth_reference") else None,
                verification_state=VerificationState(
                    str(raw.get("verification_state", "UNVERIFIED"))
                ),
                verification_timestamp=str(raw["verification_timestamp"])
                if raw.get("verification_timestamp")
                else None,
                manifest_digest=str(raw["manifest_digest"]) if raw.get("manifest_digest") else None,
            )
            if (
                raw.get("manifest_digest")
                and str(raw["manifest_digest"]) != manifest.canonical_digest
            ):
                raise ToolManifestError("manifest digest mismatch")
            return manifest
        except (KeyError, TypeError, ValueError) as exc:
            raise ToolManifestError(f"malformed tool manifest: {exc}") from exc


@dataclass(frozen=True)
class FrozenToolBinding:
    ref: str
    transport: ToolTransport
    manifest_digest: str
    artifact_path: str | None
    artifact_sha256: str | None
    command: str | None
    hint: str
    side_effect: SideEffectClass

    def __post_init__(self) -> None:
        object.__setattr__(self, "ref", validate_logical_ref(self.ref))
        if not _DIGEST_RE.fullmatch(self.manifest_digest):
            raise ToolManifestError("frozen manifest digest must be a SHA-256 hex digest")
        if self.artifact_sha256 is not None and not _DIGEST_RE.fullmatch(self.artifact_sha256):
            raise ToolManifestError("frozen artifact digest must be a SHA-256 hex digest")
        if not self.hint.strip() or len(self.hint.encode("utf-8")) > 512:
            raise ToolManifestError("frozen tool hint must be non-empty and bounded")
        _safe_path(self.artifact_path)
        if self.command is not None and (
            not self.command or any(part == ".." for part in Path(self.command).parts)
        ):
            raise ToolManifestError("invalid frozen logical tool command")

    def as_dict(self) -> dict[str, object]:
        return {
            "ref": self.ref,
            "transport": self.transport.value,
            "manifest_digest": self.manifest_digest,
            "artifact_path": self.artifact_path,
            "artifact_sha256": self.artifact_sha256,
            "command": self.command,
            "hint": self.hint,
            "side_effect": self.side_effect.value,
        }


@dataclass(frozen=True)
class FrozenToolContract:
    bindings: tuple[FrozenToolBinding, ...] = ()
    digest: str = ""

    def __post_init__(self) -> None:
        bindings = tuple(sorted(self.bindings, key=lambda item: item.ref))
        object.__setattr__(self, "bindings", bindings)
        refs = [item.ref for item in bindings]
        if len(refs) != len(set(refs)):
            raise ToolManifestError("duplicate frozen logical tool refs are not permitted")
        calculated = _digest([item.as_dict() for item in bindings])
        if not self.digest:
            object.__setattr__(self, "digest", calculated)
        elif self.digest != calculated:
            raise ToolManifestError("frozen tool contract digest mismatch")

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(item.ref for item in self.bindings)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "digest": self.digest,
            "bindings": [b.as_dict() for b in self.bindings],
        }

    def to_json(self) -> str:
        return _canonical(self.as_dict())

    @classmethod
    def from_json(cls, raw: str) -> FrozenToolContract:
        try:
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("schema_version") != 1:
                raise ToolManifestError("unsupported frozen tool contract schema")
            raw_bindings = data.get("bindings")
            if not isinstance(raw_bindings, list) or any(
                not isinstance(item, dict) for item in raw_bindings
            ):
                raise ToolManifestError("frozen tool contract bindings must be objects")
            refs = [str(item.get("ref", "")) for item in raw_bindings]
            if len(refs) != len(set(refs)):
                raise ToolManifestError("duplicate frozen logical tool refs are not permitted")
            raw_digest = data.get("digest")
            if not isinstance(raw_digest, str) or not raw_digest:
                raise ToolManifestError("frozen tool contract digest is required")
            for item in raw_bindings:
                string_fields = ("ref", "transport", "manifest_digest", "hint")
                if any(not isinstance(item.get(field), str) for field in string_fields):
                    raise ToolManifestError("frozen binding identity fields must be strings")
                if item.get("artifact_path") is not None and not isinstance(
                    item.get("artifact_path"), str
                ):
                    raise ToolManifestError("frozen artifact path must be a string")
                if item.get("artifact_sha256") is not None and not isinstance(
                    item.get("artifact_sha256"), str
                ):
                    raise ToolManifestError("frozen artifact digest must be a string")
                if item.get("command") is not None and not isinstance(item.get("command"), str):
                    raise ToolManifestError("frozen command must be a string")
                if not isinstance(item.get("side_effect", "UNKNOWN"), str):
                    raise ToolManifestError("frozen side_effect must be a string")
            bindings = tuple(
                FrozenToolBinding(
                    ref=cast(str, item["ref"]),
                    transport=ToolTransport(cast(str, item["transport"])),
                    manifest_digest=cast(str, item["manifest_digest"]),
                    artifact_path=cast(str, item["artifact_path"])
                    if item.get("artifact_path")
                    else None,
                    artifact_sha256=cast(str, item["artifact_sha256"])
                    if item.get("artifact_sha256")
                    else None,
                    command=cast(str, item["command"]) if item.get("command") else None,
                    hint=cast(str, item["hint"]),
                    side_effect=SideEffectClass(
                        cast(str, item.get("side_effect", "UNKNOWN"))
                    ),
                )
                for item in raw_bindings
            )
            contract = cls(bindings, digest=raw_digest)
            if contract.digest != _digest([item.as_dict() for item in contract.bindings]):
                raise ToolManifestError("frozen tool contract digest mismatch")
            return contract
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ToolManifestError(f"malformed frozen tool contract: {exc}") from exc


def freeze_tool_contract(refs: tuple[str, ...], registry: Any) -> FrozenToolContract:
    """Resolve refs once, producing immutable non-secret run evidence."""
    normalized = tuple(sorted({validate_logical_ref(ref) for ref in refs}))
    bindings: list[FrozenToolBinding] = []
    for ref in normalized:
        manifest, spec = registry.resolve(ref)
        if manifest.verification_state != VerificationState.VERIFIED:
            raise ToolManifestError(f"tool '{ref}' is not verified")
        if manifest.transport != ToolTransport.NATIVE_MCP and not manifest.artifact_path:
            raise ToolManifestError(f"verified tool '{ref}' has no executable artifact")
        if manifest.transport != ToolTransport.NATIVE_MCP and not manifest.artifact_sha256:
            raise ToolManifestError(f"verified tool '{ref}' has no artifact SHA-256")
        bindings.append(
            FrozenToolBinding(
                ref=ref,
                transport=manifest.transport,
                manifest_digest=manifest.manifest_digest or manifest.canonical_digest,
                artifact_path=manifest.artifact_path,
                artifact_sha256=manifest.artifact_sha256,
                command=spec.command or spec.ref.split(".", 1)[1],
                hint=spec.hint,
                side_effect=spec.side_effect,
            )
        )
    return FrozenToolContract(tuple(bindings))


def tool_context_metrics(prompt: str, contract: FrozenToolContract) -> dict[str, int]:
    """Measure only the bounded initial exposure, never generated schema bytes."""
    if not contract.bindings:
        return {
            "tool_schema_bytes": 0,
            "tool_description_bytes": 0,
            "tools_exposed_count": 0,
        }
    marker = "[AVAILABLE TOOLS]"
    start = prompt.find(marker)
    end = prompt.find("VALIDATION POLICY:", start) if start >= 0 else -1
    section = prompt[start:] if start >= 0 else ""
    if end > start:
        section = prompt[start:end]
    return {
        "tool_schema_bytes": 0,
        "tool_description_bytes": len(section.encode("utf-8")),
        "tools_exposed_count": len(contract.bindings),
    }


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
