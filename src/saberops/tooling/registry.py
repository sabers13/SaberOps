"""Deterministic manifest registry and transport selection."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from saberops.tooling.contracts import (
    ToolBundleManifest,
    ToolManifestError,
    ToolSpec,
    ToolTransport,
    VerificationState,
    validate_logical_ref,
)


def default_registry_path() -> Path:
    configured = os.environ.get("ORCH_TOOL_REGISTRY", "").strip()
    if configured:
        return Path(configured).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser()
    return config_home / "orchestrator-mvp" / "tools.json"


class ToolRegistry:
    """Read-only in-process registry; registration is explicit administrative input."""

    def __init__(self, manifests: Iterable[ToolBundleManifest] = ()) -> None:
        self._manifests = tuple(manifests)
        self._validate()

    @classmethod
    def from_path(cls, path: Path | str | None = None) -> ToolRegistry:
        resolved = Path(path).expanduser() if path is not None else default_registry_path()
        if not resolved.exists():
            return cls()
        try:
            raw = json.loads(resolved.read_text(encoding="utf-8"))
            items = raw.get("manifests", raw) if isinstance(raw, dict) else raw
            if not isinstance(items, list):
                raise ToolManifestError("tool registry must contain a manifest list")
            if any(not isinstance(item, dict) for item in items):
                raise ToolManifestError("tool registry manifests must be objects")
            return cls(
                ToolBundleManifest.from_dict(item) for item in items
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise ToolManifestError(f"cannot read tool registry: {exc}") from exc

    @classmethod
    def from_environment(cls) -> ToolRegistry:
        return cls.from_path()

    def _validate(self) -> None:
        seen_bundles: set[str] = set()
        for manifest in self._manifests:
            if manifest.bundle_id in seen_bundles:
                raise ToolManifestError(f"duplicate bundle id: {manifest.bundle_id}")
            seen_bundles.add(manifest.bundle_id)

    @property
    def manifests(self) -> tuple[ToolBundleManifest, ...]:
        return tuple(sorted(self._manifests, key=lambda item: item.bundle_id))

    def list_refs(self) -> tuple[ToolSpec, ...]:
        selected: dict[str, ToolSpec] = {}
        for manifest in self.manifests:
            for spec in manifest.tools:
                selected.setdefault(spec.ref, spec)
        return tuple(sorted(selected.values(), key=lambda item: item.ref))

    def resolve(self, ref: str) -> tuple[ToolBundleManifest, ToolSpec]:
        normalized = validate_logical_ref(ref)
        matches = [(m, s) for m in self.manifests for s in m.tools if s.ref == normalized]
        if not matches:
            raise ToolManifestError(f"unknown logical tool ref: {normalized}")
        verified = [
            pair for pair in matches if pair[0].verification_state == VerificationState.VERIFIED
        ]
        native_cli = [pair for pair in verified if pair[0].transport == ToolTransport.NATIVE_CLI]
        if native_cli:
            return native_cli[0]
        generated = [pair for pair in verified if pair[0].transport == ToolTransport.MCP_TO_CLI]
        if generated:
            return generated[0]
        native_mcp = [pair for pair in matches if pair[0].transport == ToolTransport.NATIVE_MCP]
        if native_mcp:
            return native_mcp[0]
        raise ToolManifestError(
            f"no eligible verified transport for logical tool ref: {normalized}"
        )

    def resolve_manifest(self, ref: str) -> ToolBundleManifest:
        return self.resolve(ref)[0]

    def inspect(self) -> list[dict[str, object]]:
        return [manifest.as_dict() for manifest in self.manifests]
