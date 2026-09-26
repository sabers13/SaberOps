"""R2-A project settings authority (owner-controlled run policy inputs).

The logical owner-controlled settings are:

* gate command (new persisted document; no prior authority existed)
* data policy / training use (new persisted document; unset is Denied)
* accept target branch (new persisted document; unset resolves live)
* review policy (COMPOSED from :mod:`saberops.review_adaptive` -- never copied)
* gate storage / gate-scratch preference (COMPOSED from
  :mod:`saberops.gate_runner` -- never copied)

This module owns exactly one new persisted document,
``project-settings.json`` (schema version 1), holding only the three
settings that had no canonical store.  Review policy and gate-scratch
reads/writes delegate to their existing canonical persistence so this
layer can never produce a divergent copy.

Legacy/frozen Runs are immutable: this module never rewrites
a Run's config or evidence; it only resolves policy for *new* runs.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
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


@dataclass(frozen=True)
class ProjectSettings:
    """Persisted owner decisions that had no prior canonical store.

    ``None`` means "no explicit owner decision yet" -- callers must
    apply the R2-A safe defaults rather than treating absence as consent.
    """

    gate_command: str | None = None
    data_policy: DataPolicy | None = None
    accept_target_branch: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Serialize for durable storage (only explicit decisions persist)."""
        payload: dict[str, object] = {"version": _SETTINGS_SCHEMA_VERSION}
        if self.gate_command is not None:
            payload["gate_command"] = self.gate_command
        if self.data_policy is not None:
            payload["data_policy"] = self.data_policy.value
        if self.accept_target_branch is not None:
            payload["accept_target_branch"] = self.accept_target_branch
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
            f"project settings at {source}: unknown data policy {value!r}; "
            f"expected one of {known}"
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
            f"project settings at {source}: target branch exceeds "
            f"{_MAX_BRANCH_LENGTH} characters"
        )
    if text == "HEAD" or text.startswith("-") or ".." in text or " " in text:
        raise ProjectSettingsError(
            f"project settings at {source}: invalid target branch {value!r}"
        )
    return text


def _parse_document(data: object, source: str) -> ProjectSettings:
    if not isinstance(data, dict):
        raise ProjectSettingsError(
            f"project settings at {source}: payload must be a JSON object"
        )
    version = data.get("version")
    if version != _SETTINGS_SCHEMA_VERSION:
        raise ProjectSettingsError(
            f"project settings at {source}: unknown schema version {version!r}; "
            f"expected {_SETTINGS_SCHEMA_VERSION}"
        )
    gate_raw = data.get("gate_command")
    data_raw = data.get("data_policy")
    branch_raw = data.get("accept_target_branch")
    return ProjectSettings(
        gate_command=(
            _coerce_gate_command(gate_raw, source) if gate_raw is not None else None
        ),
        data_policy=(
            _coerce_data_policy(data_raw, source) if data_raw is not None else None
        ),
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
        raise ProjectSettingsError(
            f"project settings at {path}: cannot read: {exc}"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProjectSettingsError(
            f"project settings at {path}: invalid JSON: {exc}"
        ) from exc
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
            f"cannot resolve project state directory for "
            f"{Path(repo_path).expanduser()}: {exc}"
        ) from exc


def load_project_settings_for_state_dir(state_dir: Path | str) -> ProjectSettings:
    """Return persisted owner decisions for ``state_dir`` (unset fields are None)."""
    loaded = _read_settings_file(project_settings_path(state_dir))
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
    _clear_gate: bool = False,
    _clear_branch: bool = False,
) -> Path:
    """Merge owner decisions into the persisted document and return its path.

    Only the supplied fields are updated; all others are preserved
    byte-for-byte in intent (the file is rewritten deterministically).
    Pass ``_clear_gate=True`` / ``_clear_branch=True`` to remove an
    explicit decision and return that field to unset.
    """
    current = load_project_settings_for_state_dir(state_dir)
    gate = current.gate_command
    policy = current.data_policy
    branch = current.accept_target_branch
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
    merged = ProjectSettings(
        gate_command=gate, data_policy=policy, accept_target_branch=branch
    )
    path = project_settings_path(state_dir)
    _atomic_write_json(path, merged.as_dict())
    return path


def set_project_gate_command(repo_path: Path | str, gate_command: str) -> Path:
    """Validate and persist the project's explicit gate command."""
    state_dir = _resolve_state_dir(repo_path)
    current = load_project_settings_for_state_dir(state_dir)
    merged = ProjectSettings(
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
    merged = ProjectSettings(
        gate_command=None,
        data_policy=current.data_policy,
        accept_target_branch=current.accept_target_branch,
    )
    path = project_settings_path(state_dir)
    _atomic_write_json(path, merged.as_dict())
    return path


def set_project_data_policy(repo_path: Path | str, policy: DataPolicy | str) -> Path:
    """Validate and persist the project's training/data-use policy."""
    state_dir = _resolve_state_dir(repo_path)
    current = load_project_settings_for_state_dir(state_dir)
    resolved = (
        policy if isinstance(policy, DataPolicy) else _coerce_data_policy(policy, str(repo_path))
    )
    merged = ProjectSettings(
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
    merged = ProjectSettings(
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
    merged = ProjectSettings(
        gate_command=current.gate_command,
        data_policy=current.data_policy,
        accept_target_branch=None,
    )
    path = project_settings_path(state_dir)
    _atomic_write_json(path, merged.as_dict())
    return path


def detect_authoritative_gate(repo_path: Path | str) -> str | None:
    """Return the narrow deterministic gate detection, if positively found.

    R2-A rule (deliberately narrow; no build-system heuristics): when the
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


def is_project_positively_public(repo_path: Path | str) -> bool:
    """Return True only when the project is positively established as public.

    R2-A implements no automatic public inference: no remote-URL
    heuristic, license-file probe, or hosting check can positively
    establish public training consent, so this always returns False and
    an unset data policy resolves conservatively to Denied.  The seam
    exists so a future tranche can add a narrow, auditable public
    proof without changing the safe-default call sites.
    """
    _ = repo_path
    return False


def resolve_effective_data_policy(
    settings: ProjectSettings,
    *,
    repo_path: Path | str | None = None,
) -> DataPolicy:
    """Resolve the effective data policy with the R2-A safe default.

    An explicit persisted decision always wins.  Otherwise the project
    must be positively established as public to Allow; every other
    case (private, unknown, unresolvable) is Denied.  R2-A never
    auto-establishes publicness, so unset is Denied.
    """
    if settings.data_policy is not None:
        return settings.data_policy
    if repo_path is not None and is_project_positively_public(repo_path):
        return DataPolicy.ALLOWED
    return DataPolicy.DENIED


def resolve_effective_training_allowed(
    settings: ProjectSettings,
    *,
    repo_path: Path | str | None = None,
) -> bool:
    """Map the effective data policy onto the frozen ``training_allowed`` bit."""
    return resolve_effective_data_policy(settings, repo_path=repo_path) is DataPolicy.ALLOWED


# ---------------------------------------------------------------------------
# Composed canonical authorities (delegates -- never divergent copies)
# ---------------------------------------------------------------------------


def set_project_review_policy(repo_path: Path | str, mode: object) -> Path:
    """Persist the project's review policy via the canonical store.

    Delegates to
    :func:`saberops.review_adaptive.set_project_review_policy_for_state_dir`
    so project-settings writes use the canonical existing persistence
    rather than producing a divergent copy.
    """
    from saberops.project import bind_project_state_dir, compute_project_identity
    from saberops.review_adaptive import set_project_review_policy_for_state_dir

    state_dir = bind_project_state_dir(compute_project_identity(Path(repo_path).expanduser()))
    return set_project_review_policy_for_state_dir(state_dir, mode)  # type: ignore[arg-type]


def set_project_gate_scratch_mode(repo_path: Path | str, mode: object) -> Path:
    """Persist the project's gate-scratch mode via the canonical store.

    Delegates to :func:`saberops.gate_runner.set_project_gate_scratch_policy`
    so project-settings writes use the canonical existing persistence
    rather than producing a divergent copy.
    """
    from saberops.gate_runner import set_project_gate_scratch_policy

    return set_project_gate_scratch_policy(repo_path, mode)  # type: ignore[arg-type]


__all__ = [
    "DataPolicy",
    "OWNER_DATA_POLICY_LABELS",
    "ProjectSettings",
    "ProjectSettingsError",
    "clear_project_accept_target_branch",
    "clear_project_gate_command",
    "detect_authoritative_gate",
    "is_project_positively_public",
    "load_project_settings",
    "load_project_settings_for_state_dir",
    "project_settings_path",
    "resolve_effective_data_policy",
    "resolve_effective_training_allowed",
    "set_project_accept_target_branch",
    "set_project_data_policy",
    "set_project_gate_command",
    "set_project_gate_scratch_mode",
    "set_project_review_policy",
    "set_project_settings_for_state_dir",
]
