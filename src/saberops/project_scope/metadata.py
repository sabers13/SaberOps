"""Explicit project metadata under ``.orch/`` (fail-closed).

Shape::

    .orch/project.toml      schema_version, project_id, analyzer
    .orch/modules/*.toml    id, description, roots, interfaces, tests,
                            allowed_dependencies

Explicit valid metadata outranks every inference.  A malformed authoritative
file is a deterministic failure: this loader never silently infers around a
broken contract.  Metadata is untrusted input: paths must be repository-
relative, may not escape the repository, and never define executable commands.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from saberops.project_scope.contracts import GRAPH_SCHEMA_VERSION

ORCH_DIR = ".orch"
PROJECT_FILE = f"{ORCH_DIR}/project.toml"
MODULES_SUBDIR = f"{ORCH_DIR}/modules"

_PROJECT_KEYS = frozenset({"schema_version", "project_id", "analyzer"})
_MODULE_KEYS = frozenset(
    {
        "schema_version",
        "id",
        "description",
        "roots",
        "interfaces",
        "tests",
        "allowed_dependencies",
    }
)


class ProjectMetadataError(Exception):
    """Raised when explicit project metadata is malformed or contradictory."""


@dataclass(frozen=True)
class ExplicitModule:
    """One explicitly declared module contract."""

    id: str
    description: str
    roots: tuple[str, ...]
    interfaces: tuple[str, ...]
    tests: tuple[str, ...]
    allowed_dependencies: tuple[str, ...]
    source_path: str


@dataclass(frozen=True)
class ProjectMetadata:
    """The parsed ``.orch`` project contract."""

    project_id: str
    analyzer: str
    modules: tuple[ExplicitModule, ...]

    @property
    def module_ids(self) -> tuple[str, ...]:
        """Every declared module id, sorted."""
        return tuple(sorted(module.id for module in self.modules))


def _safe_relative_path(value: str, origin: str, field_name: str) -> str:
    """Validate one repository-relative path/pattern; return it normalized."""
    if not value or value.strip() != value:
        raise ProjectMetadataError(f"{origin}: {field_name} must be a non-empty path: {value!r}")
    if "\\" in value or value.startswith("/") or PurePosixPath(value).is_absolute():
        raise ProjectMetadataError(
            f"{origin}: {field_name} must be repository-relative, got {value!r}"
        )
    parts = PurePosixPath(value).parts
    if any(part == ".." for part in parts):
        raise ProjectMetadataError(
            f"{origin}: {field_name} must not traverse outside the repository: {value!r}"
        )
    return value


def _parse_module(text: str, origin: str) -> ExplicitModule:
    """Parse one ``.orch/modules/<id>.toml`` file."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ProjectMetadataError(f"{origin}: malformed TOML: {exc}") from exc
    if not isinstance(data, dict):
        raise ProjectMetadataError(f"{origin}: top level must be a table")
    unknown = sorted(set(data) - _MODULE_KEYS)
    if unknown:
        raise ProjectMetadataError(f"{origin}: unknown key(s): {', '.join(unknown)}")
    schema_version = data.get("schema_version")
    if schema_version != GRAPH_SCHEMA_VERSION:
        raise ProjectMetadataError(
            f"{origin}: unsupported schema_version {schema_version!r}; "
            f"this build understands {GRAPH_SCHEMA_VERSION}"
        )
    module_id = data.get("id")
    if not isinstance(module_id, str) or not module_id.strip():
        raise ProjectMetadataError(f"{origin}: id must be a non-empty string")
    if module_id != Path(origin).stem:
        raise ProjectMetadataError(
            f"{origin}: id '{module_id}' must match the file stem '{Path(origin).stem}'"
        )
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ProjectMetadataError(f"{origin}: description must be a string")
    roots = _string_list(data, "roots", origin, paths=True)
    if not roots:
        raise ProjectMetadataError(f"{origin}: roots must declare at least one pattern")
    return ExplicitModule(
        id=module_id,
        description=description,
        roots=roots,
        interfaces=_string_list(data, "interfaces", origin, paths=True),
        tests=_string_list(data, "tests", origin, paths=True),
        allowed_dependencies=_string_list(data, "allowed_dependencies", origin, paths=False),
        source_path=origin,
    )


def load_project_metadata(root: Path) -> ProjectMetadata | None:
    """Load ``.orch`` metadata for ``root``; ``None`` when the project has none.

    Raises :class:`ProjectMetadataError` for malformed, contradictory or
    duplicated declarations so callers fail closed instead of inferring around
    broken authoritative files.
    """
    project_path = root / PROJECT_FILE
    if not project_path.is_file():
        if (root / MODULES_SUBDIR).is_dir():
            raise ProjectMetadataError(
                f"{MODULES_SUBDIR}/ exists but {PROJECT_FILE} is missing"
            )
        return None

    try:
        data = tomllib.loads(project_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ProjectMetadataError(f"{PROJECT_FILE}: malformed TOML: {exc}") from exc
    except OSError as exc:
        raise ProjectMetadataError(f"{PROJECT_FILE}: unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise ProjectMetadataError(f"{PROJECT_FILE}: top level must be a table")
    unknown = sorted(set(data) - _PROJECT_KEYS)
    if unknown:
        raise ProjectMetadataError(f"{PROJECT_FILE}: unknown key(s): {', '.join(unknown)}")
    if data.get("schema_version") != GRAPH_SCHEMA_VERSION:
        raise ProjectMetadataError(
            f"{PROJECT_FILE}: unsupported schema_version {data.get('schema_version')!r}; "
            f"this build understands {GRAPH_SCHEMA_VERSION}"
        )
    project_id = data.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise ProjectMetadataError(f"{PROJECT_FILE}: project_id must be a non-empty string")
    analyzer = data.get("analyzer", "python")
    if not isinstance(analyzer, str) or not analyzer.strip():
        raise ProjectMetadataError(f"{PROJECT_FILE}: analyzer must be a non-empty string")

    modules_dir = root / MODULES_SUBDIR
    if not modules_dir.is_dir():
        raise ProjectMetadataError(f"{MODULES_SUBDIR}: directory is missing")
    modules: list[ExplicitModule] = []
    seen_ids: dict[str, str] = {}
    for path in sorted(modules_dir.glob("*.toml")):
        origin = f"{MODULES_SUBDIR}/{path.name}"
        module = _parse_module(path.read_text(encoding="utf-8"), origin)
        if module.id in seen_ids:
            raise ProjectMetadataError(
                f"{origin}: duplicate module id '{module.id}' (also declared in "
                f"{seen_ids[module.id]})"
            )
        seen_ids[module.id] = origin
        modules.append(module)
    if not modules:
        raise ProjectMetadataError(f"{MODULES_SUBDIR}: no module contracts found")

    known = {module.id for module in modules}
    for module in modules:
        missing = sorted(set(module.allowed_dependencies) - known)
        if missing:
            raise ProjectMetadataError(
                f"{module.source_path}: dependency on unknown module(s): {', '.join(missing)}"
            )

    return ProjectMetadata(project_id=project_id, analyzer=analyzer, modules=tuple(modules))


def _string_list(table: dict[str, Any], key: str, origin: str, *, paths: bool) -> tuple[str, ...]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ProjectMetadataError(f"{origin}: {key} must be a list of strings")
    if any(not item.strip() for item in value):
        raise ProjectMetadataError(f"{origin}: {key} must not contain empty strings")
    duplicates = sorted({item for item in value if value.count(item) > 1})
    if duplicates:
        raise ProjectMetadataError(
            f"{origin}: {key} has duplicate entries: {', '.join(duplicates)}"
        )
    entries = tuple(value)
    if paths:
        entries = tuple(_safe_relative_path(item, origin, key) for item in entries)
    return entries
