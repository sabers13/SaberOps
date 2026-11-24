"""Project settings authority (owner-controlled run policy inputs).

The logical owner-controlled settings are:

* gate command (new persisted document; no prior authority existed)
* data policy / training use (new persisted document; unset is Denied)
* accept target branch (new persisted document; unset resolves live)
* gate storage / gate-scratch preference (COMPOSED from
  :mod:`saberops.gate_runner` -- never copied)

Independent review is mandatory and has no project setting.  This module
owns exactly one new persisted document, ``project-settings.json``
(schema version 1); gate-scratch reads/writes delegate to their existing
canonical persistence so this layer can never produce a divergent copy.

Legacy/frozen Runs are immutable: this module never rewrites
a Run's config or evidence; it only resolves policy for *new* runs.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Final


class ProjectSettingsError(RuntimeError):
    """Fail-closed error for invalid project-settings authority."""


class DataPolicy(StrEnum):
    """Owner-facing training/data-use policy vocabulary.

    * ``ALLOWED`` -- the owner explicitly permits task data to flow to
      training-eligible models.
    * ``DENIED`` -- training-eligible models are excluded.

    There is intentionally no third persisted value: absence of a
    decision is represented by ``None`` at the API layer and resolves
    conservatively to ``DENIED`` for fresh private/unknown projects.
    """

    ALLOWED = "ALLOWED"
    DENIED = "DENIED"


OWNER_DATA_POLICY_LABELS: Final[dict[DataPolicy, str]] = {
    DataPolicy.ALLOWED: "Allowed",
    DataPolicy.DENIED: "Denied",
}

_SETTINGS_FILENAME: Final[str] = "project-settings.json"
_SETTINGS_SCHEMA_VERSION: Final[int] = 1
_SETTINGS_TMP_PREFIX: Final[str] = "project-settings-"

_GATEfilenames: Final[tuple[str, ...]] = ("Makefile", "makefile", "GNUmakefile")
_GATE_TARGET_RE: Final = re.compile(r"(?m)^gate\s*:")

_MAX_GATE_COMMAND_LENGTH: Final[int] = 500
_MAX_BRANCH_LENGTH: Final[int] = 255


def project_settings_path(state_dir: Path | str) -> Path:
    """Return the on-disk path of the per-project settings document."""
    return Path(state_dir) / _SETTINGS_FILENAME


def project_state_dir(repo_path: Path | str) -> Path:
    """Return the verified per-project state directory for ``repo_path``."""
    return _resolve_state_dir(repo_path)


@dataclass(frozen=True)
class ProjectSettings:
    """Persisted owner decisions that had no prior canonical store.

    ``None`` means "no explicit owner decision yet" -- callers must
    apply the project-settings safe defaults rather than treating absence as consent.
    """

    gate_command: str | None = None
    data_policy: DataPolicy | None = None
    accept_target_branch: str | None = None
    gate_enabled: bool = True
    #: ``cheap`` (default), ``build``, or one exact ``provider/model`` on
    #: the build route.  ``None`` means the documented cheap default.
    scout_route: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Serialize for durable storage (only explicit decisions persist)."""
        payload: dict[str, object] = {"version": _SETTINGS_SCHEMA_VERSION}
        if self.gate_command is not None:
            payload["gate_command"] = self.gate_command
        if self.data_policy is not None:
            payload["data_policy"] = self.data_policy.value
        if self.accept_target_branch is not None:
            payload["accept_target_branch"] = self.accept_target_branch
        payload.update(gate_enabled=self.gate_enabled)
        if self.scout_route is not None:
            payload["scout_route"] = self.scout_route
        return payload


def _coerce_data_policy(value: object, source: str) -> DataPolicy:
    token = str(value).strip().upper()
    if token in ("ALLOWED", "ALLOW", "TRUE", "YES", "1"):
        return DataPolicy.ALLOWED
    if token in ("DENIED", "DENY", "FALSE", "NO", "0"):
        return DataPolicy.DENIED
    try:
        return DataPolicy(token)
    except ValueError as exc:
        known = ", ".join(m.value for m in DataPolicy)
        raise ProjectSettingsError(
            f"project settings at {source}: unknown data policy {value!r}; expected one of {known}"
        ) from exc


def _coerce_gate_command(value: object, source: str) -> str:
    text = str(value).strip()
    if not text:
        raise ProjectSettingsError(f"project settings at {source}: gate command is empty")
    if len(text) > _MAX_GATE_COMMAND_LENGTH:
        raise ProjectSettingsError(
            f"project settings at {source}: gate command exceeds "
            f"{_MAX_GATE_COMMAND_LENGTH} characters"
        )
    return text


def _coerce_branch(value: object, source: str) -> str:
    text = str(value).strip()
    if not text:
        raise ProjectSettingsError(f"project settings at {source}: target branch is empty")
    if len(text) > _MAX_BRANCH_LENGTH:
        raise ProjectSettingsError(
            f"project settings at {source}: target branch exceeds {_MAX_BRANCH_LENGTH} characters"
        )
    if text == "HEAD" or text.startswith("-") or ".." in text or " " in text:
        raise ProjectSettingsError(f"project settings at {source}: invalid target branch {value!r}")
    return text


def _coerce_scout_route(value: object, source: str) -> str:
    """Validate the scout-route setting (``cheap``, ``build`` or route id)."""
    from saberops.scout_routing import validate_scout_route

    try:
        return validate_scout_route(str(value))
    except ValueError as exc:
        raise ProjectSettingsError(f"project settings at {source}: {exc}") from exc


def _parse_document(data: object, source: str) -> ProjectSettings:
    if not isinstance(data, dict):
        raise ProjectSettingsError(f"project settings at {source}: payload must be a JSON object")
    version = data.get("version")
    if version != _SETTINGS_SCHEMA_VERSION:
        raise ProjectSettingsError(
            f"project settings at {source}: unknown schema version {version!r}; "
            f"expected {_SETTINGS_SCHEMA_VERSION}"
        )
    gate_raw = data.get("gate_command")
    data_raw = data.get("data_policy")
    branch_raw = data.get("accept_target_branch")
    gate_enabled = data.get("gate_enabled", True)
    scout_route = data.get("scout_route")
    if type(gate_enabled) is not bool:
        raise ProjectSettingsError("Invalid project workflow policy")
    _warn_retired_review_fields(data, source)
    return ProjectSettings(
        gate_enabled=gate_enabled,
        scout_route=(
            _coerce_scout_route(scout_route, source) if scout_route is not None else None
        ),
        gate_command=(_coerce_gate_command(gate_raw, source) if gate_raw is not None else None),
        data_policy=(_coerce_data_policy(data_raw, source) if data_raw is not None else None),
        accept_target_branch=(
            _coerce_branch(branch_raw, source) if branch_raw is not None else None
        ),
    )


def _read_settings_file(path: Path) -> ProjectSettings | None:
    """Read the settings document; ``None`` when no owner decision exists yet.

    A present-but-corrupt file raises :class:`ProjectSettingsError` so the
    caller fails closed rather than silently reverting to defaults.
    """
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProjectSettingsError(f"project settings at {path}: cannot read: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProjectSettingsError(f"project settings at {path}: invalid JSON: {exc}") from exc
    return _parse_document(data, str(path))


def _resolve_state_dir(repo_path: Path | str) -> Path:
    from saberops.project import (
        ProjectIdentityError,
        bind_project_state_dir,
        compute_project_identity,
    )

    repo = Path(repo_path).expanduser()
    try:
        identity = compute_project_identity(repo)
        return bind_project_state_dir(identity)
    except ProjectIdentityError as exc:
        raise ProjectSettingsError(
            f"cannot resolve project state directory for {Path(repo_path).expanduser()}: {exc}"
        ) from exc


_RETIRED_REVIEW_KEYS: Final[tuple[str, ...]] = (
    "review_disabled",
    "review_mode",
    "review_limit",
)

#: One notice per retired review setting per process (a store can be read on
#: every scheduler tick).
_warned_retired_review: set[str] = set()


def _warn_retired_review_fields(data: Mapping[str, object], source: str) -> None:
    """Print one migration notice when a store still carries review settings."""
    if not any(key in data for key in _RETIRED_REVIEW_KEYS):
        return
    if source in _warned_retired_review:
        return
    _warned_retired_review.add(source)
    print(
        "[!] Independent review is always on; the project review settings in "
        f"{source} are retired and ignored.",
        file=sys.stderr,
    )


def load_project_settings_for_state_dir(state_dir: Path | str) -> ProjectSettings:
    """Return persisted owner decisions for ``state_dir`` (unset fields are None)."""
    loaded = _read_settings_file(project_settings_path(state_dir))
    legacy_policy = Path(state_dir) / "review-policy.json"
    if legacy_policy.exists() and str(state_dir) not in _warned_retired_review:
        _warned_retired_review.add(str(state_dir))
        print(
            "[!] Independent review is always on; the project review policy "
            f"at {legacy_policy} is retired and ignored.",
            file=sys.stderr,
        )
    return ProjectSettings() if loaded is None else loaded


def load_project_settings(repo_path: Path | str) -> ProjectSettings:
    """Return persisted owner decisions for the project at ``repo_path``."""
    return load_project_settings_for_state_dir(_resolve_state_dir(repo_path))


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp_path: Path | None = None
    fd: int | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=_SETTINGS_TMP_PREFIX, suffix=".tmp", dir=str(path.parent)
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        fd = None
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


class _UnsetType:
    """Sentinel marking 'argument not supplied' for partial settings updates."""

    __slots__ = ()


_UNSET: Final[_UnsetType] = _UnsetType()


def set_project_settings_for_state_dir(
    state_dir: Path | str,
    *,
    gate_command: str | None | _UnsetType = _UNSET,
    data_policy: DataPolicy | str | None = None,
    accept_target_branch: str | None = None,
    scout_route: str | None = None,
    _clear_gate: bool = False,
    _clear_branch: bool = False,
    _clear_scout_route: bool = False,
) -> Path:
    """Merge owner decisions into the persisted document and return its path.

    Only the supplied fields are updated; all others are preserved
    byte-for-byte in intent (the file is rewritten deterministically).
    Pass ``_clear_gate=True`` / ``_clear_branch=True`` /
    ``_clear_scout_route=True`` to remove an explicit decision and return that
    field to unset.
    """
    current = load_project_settings_for_state_dir(state_dir)
    gate = current.gate_command
    policy = current.data_policy
    branch = current.accept_target_branch
    scout = current.scout_route
    if gate_command is not _UNSET:
        if _clear_gate or gate_command is None:
            gate = None
        else:
            gate = _coerce_gate_command(gate_command, str(state_dir))
    if data_policy is not None:
        policy = (
            data_policy
            if isinstance(data_policy, DataPolicy)
            else _coerce_data_policy(data_policy, str(state_dir))
        )
    if _clear_branch:
        branch = None
    elif accept_target_branch is not None:
        branch = _coerce_branch(accept_target_branch, str(state_dir))
    if _clear_scout_route:
        scout = None
    elif scout_route is not None:
        scout = _coerce_scout_route(scout_route, str(state_dir))
    merged = replace(
        current,
        gate_command=gate,
        data_policy=policy,
        accept_target_branch=branch,
        scout_route=scout,
    )
    path = project_settings_path(state_dir)
    _atomic_write_json(path, merged.as_dict())
    return path


def set_project_gate_command(repo_path: Path | str, gate_command: str) -> Path:
    """Validate and persist the project's explicit gate command."""
    state_dir = _resolve_state_dir(repo_path)
    current = load_project_settings_for_state_dir(state_dir)
    merged = replace(
        current,
        gate_command=_coerce_gate_command(gate_command, str(repo_path)),
        data_policy=current.data_policy,
        accept_target_branch=current.accept_target_branch,
    )
    path = project_settings_path(state_dir)
    _atomic_write_json(path, merged.as_dict())
    return path


def clear_project_gate_command(repo_path: Path | str) -> Path:
    """Remove the explicit gate decision (field returns to unset)."""
    state_dir = _resolve_state_dir(repo_path)
    current = load_project_settings_for_state_dir(state_dir)
    merged = replace(
        current,
        gate_command=None,
        data_policy=current.data_policy,
        accept_target_branch=current.accept_target_branch,
    )
    path = project_settings_path(state_dir)
    _atomic_write_json(path, merged.as_dict())
    return path


def set_project_scout_route(repo_path: Path | str, route: str) -> Path:
    """Persist the project's scout route (``cheap``, ``build`` or a route id)."""
    state_dir = _resolve_state_dir(repo_path)
    return set_project_settings_for_state_dir(
        state_dir, scout_route=_coerce_scout_route(route, str(repo_path))
    )


def clear_project_scout_route(repo_path: Path | str) -> Path:
    """Remove the explicit scout-route decision (returns to the cheap default)."""
    state_dir = _resolve_state_dir(repo_path)
    return set_project_settings_for_state_dir(state_dir, _clear_scout_route=True)


def set_project_data_policy(repo_path: Path | str, policy: DataPolicy | str) -> Path:
    """Validate and persist the project's training/data-use policy."""
    state_dir = _resolve_state_dir(repo_path)
    current = load_project_settings_for_state_dir(state_dir)
    resolved = (
        policy if isinstance(policy, DataPolicy) else _coerce_data_policy(policy, str(repo_path))
    )
    merged = replace(
        current,
        gate_command=current.gate_command,
        data_policy=resolved,
        accept_target_branch=current.accept_target_branch,
    )
    path = project_settings_path(state_dir)
    _atomic_write_json(path, merged.as_dict())
    return path


def set_project_accept_target_branch(repo_path: Path | str, branch: str) -> Path:
    """Validate and persist the owner-selectable accept target branch."""
    state_dir = _resolve_state_dir(repo_path)
    current = load_project_settings_for_state_dir(state_dir)
    merged = replace(
        current,
        gate_command=current.gate_command,
        data_policy=current.data_policy,
        accept_target_branch=_coerce_branch(branch, str(repo_path)),
    )
    path = project_settings_path(state_dir)
    _atomic_write_json(path, merged.as_dict())
    return path


def clear_project_accept_target_branch(repo_path: Path | str) -> Path:
    """Remove the explicit target-branch decision (field returns to unset)."""
    state_dir = _resolve_state_dir(repo_path)
    current = load_project_settings_for_state_dir(state_dir)
    merged = replace(
        current,
        gate_command=current.gate_command,
        data_policy=current.data_policy,
        accept_target_branch=None,
    )
    path = project_settings_path(state_dir)
    _atomic_write_json(path, merged.as_dict())
    return path


def set_project_notes(repo_path: Path | str, text: str | None) -> Path:
    """Replace the per-project notes file handed to every worker attempt.

    ``None`` (or whitespace only) clears the note.  The file is stored in the
    project state directory, outside the repository, so notes are never
    committed into the reviewed diff.
    """
    from saberops.job_evidence import write_project_notes

    state_dir = _resolve_state_dir(repo_path)
    return write_project_notes(state_dir, text)


def load_project_notes(repo_path: Path | str) -> str | None:
    """Return the per-project note currently handed to worker attempts."""
    from saberops.job_evidence import read_project_notes

    return read_project_notes(_resolve_state_dir(repo_path))


def detect_authoritative_gate(repo_path: Path | str) -> str | None:
    """Return the narrow deterministic gate detection, if positively found.

    Rule (deliberately narrow; no build-system heuristics): when the
    repository root contains a Makefile variant (``Makefile``,
    ``makefile``, ``GNUmakefile``) that defines a ``gate:`` target, the
    authoritative gate is ``make gate``.  Anything else -- missing file,
    unreadable file, or no ``gate`` target -- returns ``None`` so the
    caller fails with the actionable gate-required error instead of
    silently defaulting.
    """
    root = Path(repo_path).expanduser()
    for name in _GATEfilenames:
        candidate = root / name
        try:
            if not candidate.is_file():
                continue
            text = candidate.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        if _GATE_TARGET_RE.search(text):
            return "make gate"
        return None
    return None


# ---------------------------------------------------------------------------
# Composed canonical authorities (delegates -- never divergent copies)
# ---------------------------------------------------------------------------


def set_project_gate_enabled(repo_path: Path | str, enabled: bool) -> Path:
    """Persist whether the project's authoritative gate runs for future runs."""
    if type(enabled) is not bool:
        raise ProjectSettingsError("gate_enabled must be a boolean")
    state_dir = _resolve_state_dir(repo_path)
    current = load_project_settings_for_state_dir(state_dir)
    merged = replace(current, gate_enabled=enabled)
    path = project_settings_path(state_dir)
    _atomic_write_json(path, merged.as_dict())
    return path


__all__ = [
    "DataPolicy",
    "OWNER_DATA_POLICY_LABELS",
    "ProjectSettings",
    "ProjectSettingsError",
    "clear_project_accept_target_branch",
    "clear_project_gate_command",
    "clear_project_scout_route",
    "detect_authoritative_gate",
    "load_project_settings",
    "load_project_settings_for_state_dir",
    "project_settings_path",
    "set_project_accept_target_branch",
    "set_project_data_policy",
    "set_project_gate_command",
    "set_project_gate_enabled",
    "set_project_scout_route",
    "set_project_settings_for_state_dir",
]
