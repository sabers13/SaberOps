"""Logical module contracts: the machine-readable ownership and policy layer.

A contract declares *architecture*: which paths a logical module owns, what it
exposes, which other modules it may depend on, and which changes are broad
enough to demand the authoritative full gate.  It deliberately does not
enumerate the import graph -- that is derived from source by :mod:`.graph`, so
metadata and reality can be compared rather than assumed to agree.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator_mvp.devscope.paths import MODULES_DIR, repo_root

#: Contract schema understood by this build of devscope.
SCHEMA_VERSION = 1

_TOP_LEVEL_KEYS = frozenset(
    {"schema_version", "module", "paths", "interface", "dependencies", "gate"}
)
_MODULE_KEYS = frozenset({"name", "description", "risk", "keywords"})
_PATHS_KEYS = frozenset({"owned", "tests", "context_docs"})
_INTERFACE_KEYS = frozenset({"names", "sources"})
_DEPENDENCIES_KEYS = frozenset({"allowed", "transitional"})
_GATE_KEYS = frozenset({"full_gate_triggers"})
_RISK_LEVELS = ("low", "medium", "high")


class ManifestError(Exception):
    """Raised when a module contract is malformed.

    The message always names the offending file so a failure is actionable
    without re-reading the whole architecture directory.
    """


@dataclass(frozen=True)
class ModuleManifest:
    """One logical module's architectural contract."""

    schema_version: int
    name: str
    description: str
    risk: str
    keywords: tuple[str, ...]
    owned_paths: tuple[str, ...]
    test_paths: tuple[str, ...]
    context_docs: tuple[str, ...]
    interface_names: tuple[str, ...]
    interface_sources: tuple[str, ...]
    allowed_dependencies: tuple[str, ...]
    transitional_dependencies: tuple[str, ...]
    full_gate_triggers: tuple[str, ...]
    source: str

    @property
    def declared_dependencies(self) -> tuple[str, ...]:
        """Every module this contract permits as a dependency target."""
        return tuple(sorted({*self.allowed_dependencies, *self.transitional_dependencies}))


def _require_table(
    data: dict[str, Any], key: str, allowed: frozenset[str], origin: str
) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ManifestError(f"{origin}: [{key}] must be a table")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ManifestError(f"{origin}: unknown key(s) in [{key}]: {', '.join(unknown)}")
    return value


def _string_list(table: dict[str, Any], key: str, origin: str, section: str) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ManifestError(f"{origin}: [{section}].{key} must be a list of strings")
    entries = [item for item in value if isinstance(item, str)]
    if any(not item.strip() for item in entries):
        raise ManifestError(f"{origin}: [{section}].{key} must not contain empty strings")
    duplicates = sorted({item for item in entries if entries.count(item) > 1})
    if duplicates:
        raise ManifestError(
            f"{origin}: [{section}].{key} has duplicate entries: {', '.join(duplicates)}"
        )
    return tuple(entries)


def parse_manifest(text: str, origin: str) -> ModuleManifest:
    """Parse one contract from TOML text, failing loudly on any deviation."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(f"{origin}: invalid TOML: {exc}") from exc

    unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ManifestError(f"{origin}: unknown top-level key(s): {', '.join(unknown)}")

    schema_version = data.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise ManifestError(f"{origin}: schema_version must be an integer")
    if schema_version != SCHEMA_VERSION:
        raise ManifestError(
            f"{origin}: unsupported schema_version {schema_version} (expected {SCHEMA_VERSION})"
        )

    module = _require_table(data, "module", _MODULE_KEYS, origin)
    name = module.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ManifestError(f"{origin}: [module].name must be a non-empty string")
    description = module.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ManifestError(f"{origin}: [module].description must be a non-empty string")
    risk = module.get("risk", "medium")
    if risk not in _RISK_LEVELS:
        raise ManifestError(f"{origin}: [module].risk must be one of {', '.join(_RISK_LEVELS)}")

    paths = _require_table(data, "paths", _PATHS_KEYS, origin)
    owned = _string_list(paths, "owned", origin, "paths")
    if not owned:
        raise ManifestError(f"{origin}: [paths].owned must declare at least one pattern")

    interface = _require_table(data, "interface", _INTERFACE_KEYS, origin)
    dependencies = _require_table(data, "dependencies", _DEPENDENCIES_KEYS, origin)
    gate = _require_table(data, "gate", _GATE_KEYS, origin)

    allowed = _string_list(dependencies, "allowed", origin, "dependencies")
    transitional = _string_list(dependencies, "transitional", origin, "dependencies")
    if name in allowed or name in transitional:
        raise ManifestError(f"{origin}: [dependencies] must not list the module itself")
    both = sorted(set(allowed) & set(transitional))
    if both:
        raise ManifestError(
            f"{origin}: dependency declared both allowed and transitional: {', '.join(both)}"
        )

    return ModuleManifest(
        schema_version=schema_version,
        name=name,
        description=description,
        risk=risk,
        keywords=_string_list(module, "keywords", origin, "module"),
        owned_paths=owned,
        test_paths=_string_list(paths, "tests", origin, "paths"),
        context_docs=_string_list(paths, "context_docs", origin, "paths"),
        interface_names=_string_list(interface, "names", origin, "interface"),
        interface_sources=_string_list(interface, "sources", origin, "interface"),
        allowed_dependencies=allowed,
        transitional_dependencies=transitional,
        full_gate_triggers=_string_list(gate, "full_gate_triggers", origin, "gate"),
        source=origin,
    )


def load_manifests(root: Path | None = None) -> tuple[ModuleManifest, ...]:
    """Load every module contract under ``architecture/modules``, name-sorted.

    Cross-contract consistency (unknown dependency targets, duplicate names,
    file/module name disagreement) is checked here so that every consumer of
    the manifest set can assume a coherent module namespace.
    """
    base = (root if root is not None else repo_root()) / MODULES_DIR
    if not base.is_dir():
        raise ManifestError(f"{MODULES_DIR}: architecture module directory is missing")

    manifests: list[ModuleManifest] = []
    for path in sorted(base.glob("*.toml")):
        origin = f"{MODULES_DIR}/{path.name}"
        manifest = parse_manifest(path.read_text(encoding="utf-8"), origin)
        if manifest.name != path.stem:
            raise ManifestError(f"{origin}: [module].name '{manifest.name}' must match file stem")
        manifests.append(manifest)

    if not manifests:
        raise ManifestError(f"{MODULES_DIR}: no module contracts found")

    known = {manifest.name for manifest in manifests}
    for manifest in manifests:
        missing = sorted(set(manifest.declared_dependencies) - known)
        if missing:
            raise ManifestError(
                f"{manifest.source}: dependency on unknown module(s): {', '.join(missing)}"
            )
    return tuple(sorted(manifests, key=lambda item: item.name))
