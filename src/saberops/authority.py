"""Owner-selectable authority presets: the one canonical semantic source.

Authority answers exactly one question: *which workflow actions may proceed
without asking the owner first*.  It is deliberately separate from routing
tier, workflow stage, model choice, training policy, quota state, capability
profile, and review independence.  Changing authority never reranks routing
candidates and never alters tier semantics.

The chain is::

    AuthorityMode -> AuthorityPolicy -> persisted owner setting -> ORX agent

The generated OpenCode agent Markdown is an *adapter* surface, never the
source of truth. Every permission decision is derived from the typed policy
table below without parsing prose.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Final

SCHEMA_VERSION: Final[int] = 1


class AuthorityMode(StrEnum):
    """The five owner-selectable authority presets."""

    ASK_BEFORE_ACTIONS = "ask_before_actions"
    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"
    FULL_AUTONOMY = "full_autonomy"
    FULL_AUTONOMY_PUBLISH = "full_autonomy_publish"


class HostAccessMode(StrEnum):
    """Technical host capability, independent from autonomous authority."""

    GOVERNED = "governed"
    UNRESTRICTED = "unrestricted"


# Stable product order.  UI dropdowns and `orch authority list` both render
# exactly this sequence; it is part of the product vocabulary, not an
# implementation detail.
AUTHORITY_ORDER: Final[tuple[AuthorityMode, ...]] = (
    AuthorityMode.ASK_BEFORE_ACTIONS,
    AuthorityMode.SUPERVISED,
    AuthorityMode.AUTONOMOUS,
    AuthorityMode.FULL_AUTONOMY,
    AuthorityMode.FULL_AUTONOMY_PUBLISH,
)

AUTHORITY_LABELS: Final[dict[AuthorityMode, str]] = {
    AuthorityMode.ASK_BEFORE_ACTIONS: "Ask before actions",
    AuthorityMode.SUPERVISED: "Supervised",
    AuthorityMode.AUTONOMOUS: "Autonomous",
    AuthorityMode.FULL_AUTONOMY: "Full autonomy",
    AuthorityMode.FULL_AUTONOMY_PUBLISH: "Full autonomy + publish",
}

# The packaged fallback stays conservative on purpose: a fresh installation
# must never inherit one owner's Full-autonomy selection.  The local owner
# value is an explicit `orch authority set`, persisted separately.
PACKAGE_DEFAULT_MODE: Final[AuthorityMode] = AuthorityMode.SUPERVISED

# Orch has no governed publication/release capability yet.  `auto_publish`
# is therefore a policy grant only: with no governed command to authorize,
# `full_autonomy_publish` still stops at the strongest supported accepted
# state instead of inventing raw `git push` / `git merge` / `git tag`
# authority.  When a governed command lands, name it here.
GOVERNED_PUBLISH_COMMAND: Final[str | None] = None


class AuthorityConfigError(ValueError):
    """Fail-closed error for an unknown mode or a malformed authority config."""


@dataclass(frozen=True)
class AuthorityPolicy:
    """Effective permission booleans for one authority mode.

    Every dimension is an explicit typed field.  Nothing here is inferred
    from prose, and no dimension is derived from another at read time.
    """

    auto_start: bool
    auto_retry: bool
    auto_repair: bool
    auto_escalate_when_policy_justifies: bool
    auto_gate: bool
    auto_review: bool
    auto_accept: bool
    auto_safe_cleanup: bool
    auto_publish: bool
    # Separate remote-mutation dimension: whether a *verified candidate
    # branch* (never the accepted target branch) is published to the remote
    # for external review.  Deliberately distinct from ``auto_publish``,
    # which remains a distinct policy dimension.
    auto_publish_candidate: bool
    routine_questions: bool

    def as_dict(self) -> dict[str, bool]:
        """Return the policy as a plain JSON-compatible mapping."""
        return {key: bool(value) for key, value in asdict(self).items()}


# One explicit policy table.  Read it as the product specification: each row
# is written out in full rather than computed, so a reviewer can diff two
# presets by eye.
AUTHORITY_PRESETS: Final[dict[AuthorityMode, AuthorityPolicy]] = {
    # Every mutating or cost-bearing action waits for the owner.
    AuthorityMode.ASK_BEFORE_ACTIONS: AuthorityPolicy(
        auto_start=False,
        auto_retry=False,
        auto_repair=False,
        auto_escalate_when_policy_justifies=False,
        auto_gate=False,
        auto_review=False,
        auto_accept=False,
        auto_safe_cleanup=False,
        auto_publish=False,
        auto_publish_candidate=False,
        routine_questions=True,
    ),
    # The requested run, its gate, and its review proceed; recovery work
    # (retry / repair / escalation) is what needs supervision.
    AuthorityMode.SUPERVISED: AuthorityPolicy(
        auto_start=True,
        auto_retry=False,
        auto_repair=False,
        auto_escalate_when_policy_justifies=False,
        auto_gate=True,
        auto_review=True,
        auto_accept=False,
        auto_safe_cleanup=False,
        auto_publish=False,
        auto_publish_candidate=False,
        routine_questions=True,
    ),
    # Converge on a verified candidate unattended, then stop for acceptance.
    AuthorityMode.AUTONOMOUS: AuthorityPolicy(
        auto_start=True,
        auto_retry=True,
        auto_repair=True,
        auto_escalate_when_policy_justifies=True,
        auto_gate=True,
        auto_review=True,
        auto_accept=False,
        auto_safe_cleanup=True,
        auto_publish=False,
        auto_publish_candidate=True,
        routine_questions=False,
    ),
    # Everything required to converge *and* accept, but never to publish.
    AuthorityMode.FULL_AUTONOMY: AuthorityPolicy(
        auto_start=True,
        auto_retry=True,
        auto_repair=True,
        auto_escalate_when_policy_justifies=True,
        auto_gate=True,
        auto_review=True,
        auto_accept=True,
        auto_safe_cleanup=True,
        auto_publish=False,
        auto_publish_candidate=True,
        routine_questions=False,
    ),
    # Identical to full_autonomy except for the publication grant itself.
    AuthorityMode.FULL_AUTONOMY_PUBLISH: AuthorityPolicy(
        auto_start=True,
        auto_retry=True,
        auto_repair=True,
        auto_escalate_when_policy_justifies=True,
        auto_gate=True,
        auto_review=True,
        auto_accept=True,
        auto_safe_cleanup=True,
        auto_publish=True,
        auto_publish_candidate=True,
        routine_questions=False,
    ),
}


def parse_authority_mode(value: object) -> AuthorityMode:
    """Resolve an exact mode ID, failing closed on anything unrecognized.

    Matching is exact: no case folding, no aliases, no nearest-neighbour
    guess.  An unknown value never degrades into a more permissive mode.
    """
    if not isinstance(value, str):
        raise AuthorityConfigError(f"Authority mode must be a string, got {type(value).__name__}")
    for mode in AUTHORITY_ORDER:
        if value == mode.value:
            return mode
    known = ", ".join(mode.value for mode in AUTHORITY_ORDER)
    raise AuthorityConfigError(f"Unknown authority mode {value!r}; known modes: {known}")


def policy_for(mode: AuthorityMode) -> AuthorityPolicy:
    """Return the frozen policy row for a mode."""
    return AUTHORITY_PRESETS[mode]


def label_for(mode: AuthorityMode) -> str:
    """Return the stable user-facing label for a mode."""
    return AUTHORITY_LABELS[mode]


def list_presets() -> list[dict[str, Any]]:
    """Describe all five presets in the exact stable product order."""
    return [
        {
            "mode": mode.value,
            "label": AUTHORITY_LABELS[mode],
            "policy": AUTHORITY_PRESETS[mode].as_dict(),
        }
        for mode in AUTHORITY_ORDER
    ]


def get_authority_config_path() -> Path:
    """Resolve the owner authority config path via XDG_CONFIG_HOME or ~/.config.

    This mirrors :func:`saberops.routing_config.get_user_routing_path`
    so both owner settings live in the same directory.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg and xdg.strip():
        base = Path(xdg.strip()).expanduser()
    else:
        base = Path.home() / ".config"
    return base / "orchestrator-v2" / "authority.json"


def _parse_host_access(value: object) -> HostAccessMode:
    if not isinstance(value, str):
        raise AuthorityConfigError("host_access must be a string")
    try:
        return HostAccessMode(value)
    except ValueError as exc:
        raise AuthorityConfigError(f"Unknown host_access mode {value!r}") from exc


def _validate_document(data: object, source_label: str) -> tuple[AuthorityMode, HostAccessMode]:
    if not isinstance(data, dict):
        raise AuthorityConfigError(
            f"Authority config at {source_label}: top-level must be a JSON object"
        )
    allowed_keys = {"version", "mode", "host_access"}
    unknown = set(data.keys()) - allowed_keys
    if unknown:
        raise AuthorityConfigError(
            f"Authority config at {source_label}: unknown keys {sorted(unknown)}"
        )
    if "version" not in data:
        raise AuthorityConfigError(f"Authority config at {source_label}: missing 'version'")
    version = data["version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise AuthorityConfigError(
            f"Authority config at {source_label}: version must be integer "
            f"{SCHEMA_VERSION}, got {version!r}"
        )
    if "mode" not in data:
        raise AuthorityConfigError(f"Authority config at {source_label}: missing 'mode'")
    try:
        mode = parse_authority_mode(data["mode"])
    except AuthorityConfigError as exc:
        raise AuthorityConfigError(f"Authority config at {source_label}: {exc}") from exc
    host_access = _parse_host_access(data.get("host_access", HostAccessMode.GOVERNED.value))
    return mode, host_access


@dataclass(frozen=True)
class AuthoritySettings:
    """The effective owner authority selection and where it came from."""

    mode: AuthorityMode
    source: str  # "owner" when a config file was read, else "package_default"
    source_path: str | None
    host_access: HostAccessMode = HostAccessMode.GOVERNED

    @property
    def label(self) -> str:
        """Stable user-facing label for the effective mode."""
        return AUTHORITY_LABELS[self.mode]

    @property
    def policy(self) -> AuthorityPolicy:
        """Effective policy for the effective mode."""
        return AUTHORITY_PRESETS[self.mode]

    def as_dict(self) -> dict[str, Any]:
        """Serialize the settings, label, and effective policy booleans."""
        return {
            "mode": self.mode.value,
            "label": self.label,
            "source": self.source,
            "source_path": self.source_path,
            "host_access": self.host_access.value,
            "package_default_mode": PACKAGE_DEFAULT_MODE.value,
            "config_path": str(get_authority_config_path()),
            "policy": self.policy.as_dict(),
        }


def load_authority_settings() -> AuthoritySettings:
    """Load the effective authority selection, failing closed on corruption.

    A missing config file is not an error: the conservative packaged default
    applies.  A file that exists but cannot be read, parsed, or validated
    raises rather than silently falling back to any mode.
    """
    path = get_authority_config_path()
    if not path.is_file():
        return AuthoritySettings(
            mode=PACKAGE_DEFAULT_MODE, source="package_default", source_path=None
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AuthorityConfigError(f"Authority config at {path}: cannot read: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuthorityConfigError(f"Authority config at {path}: invalid JSON: {exc}") from exc
    mode, host_access = _validate_document(data, str(path))
    return AuthoritySettings(
        mode=mode, source="owner", source_path=str(path), host_access=host_access
    )


def load_authority_mode() -> AuthorityMode:
    """Return just the effective mode (see :func:`load_authority_settings`)."""
    return load_authority_settings().mode


def load_authority_policy() -> AuthorityPolicy:
    """Return the effective policy for the persisted owner selection."""
    return load_authority_settings().policy


def set_authority_mode(mode: AuthorityMode | str) -> Path:
    """Validate and atomically persist the owner authority selection.

    The mode is validated before any filesystem work, and the document is
    written to a temporary file in the destination directory before a single
    :func:`os.replace`.  A failure therefore leaves the previous
    authoritative config byte-for-byte intact and removes the temporary file.
    """
    resolved = mode if isinstance(mode, AuthorityMode) else parse_authority_mode(mode)
    path = get_authority_config_path()
    current = load_authority_settings()
    payload_data: dict[str, object] = {
        "version": SCHEMA_VERSION,
        "mode": resolved.value,
    }
    if current.source == "owner":
        payload_data["host_access"] = current.host_access.value
    payload = json.dumps(payload_data, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".authority-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    return path


def set_host_access(mode: HostAccessMode | str) -> Path:
    """Persist technical host access while preserving authority mode."""
    resolved = mode if isinstance(mode, HostAccessMode) else _parse_host_access(mode)
    current = load_authority_settings()
    path = get_authority_config_path()
    payload = (
        json.dumps(
            {
                "version": SCHEMA_VERSION,
                "mode": current.mode.value,
                "host_access": resolved.value,
            },
            indent=2,
        )
        + "\n"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".authority-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    return path
