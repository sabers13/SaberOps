"""Canonical SaberOps runtime state/config/cache namespace.

Public SaberOps owns its own XDG namespace::

    $XDG_STATE_HOME/saberops/    (fallback ``~/.local/state/saberops/``)
    $XDG_CONFIG_HOME/saberops/   (fallback ``~/.config/saberops/``)
    $XDG_CACHE_HOME/saberops/    (fallback ``~/.cache/saberops/``)

This is the single source of truth for every default runtime path.  No
other module may hard-code an application directory name.

Legacy private-development locations (``orchestrator-mvp`` under state,
``orchestrator-v2`` / ``orchestrator-mvp`` under config) are never
selected as defaults.  They are listed explicitly via
:func:`legacy_state_dirs` / :func:`legacy_config_dirs` for diagnostics
only.  SaberOps never copies, mutates, or deletes legacy state
automatically; a fresh public install defaults to the new namespace and
never silently inherits the old private database or configuration.

Explicit ``XDG_*`` environment overrides keep working predictably: they
change the *base* directory, while the ``saberops`` segment stays fixed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Final

STATE_APP_DIR: Final[str] = "saberops"
CONFIG_APP_DIR: Final[str] = "saberops"
CACHE_APP_DIR: Final[str] = "saberops"
PROFILE_ENV: Final[str] = "SABEROPS_PROFILE"

LEGACY_STATE_APP_DIRS: Final[tuple[str, ...]] = ("orchestrator-mvp",)
LEGACY_CONFIG_APP_DIRS: Final[tuple[str, ...]] = ("orchestrator-v2", "orchestrator-mvp")

_PROJECTS_DIR: Final[str] = "projects"
_PROFILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")


def active_profile() -> str:
    """Return the optional isolated owner profile selected for this process.

    Profiles are a narrow first-user/dogfood isolation seam: they change only
    the SaberOps state/config/cache namespace and never change repository
    identity, routing semantics, or execution authority.  An invalid profile
    fails closed instead of becoming an arbitrary filesystem path.
    """
    raw = os.environ.get(PROFILE_ENV, "").strip()
    if not raw:
        return ""
    if _PROFILE_RE.fullmatch(raw) is None:
        raise ValueError(
            f"{PROFILE_ENV} must match [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}"
        )
    return raw


def _profiled_app_dir(base: Path, app_dir: str) -> Path:
    root = base / app_dir
    profile = active_profile()
    return root if not profile else root / "profiles" / profile


def state_home() -> Path:
    """Return the XDG state base (``$XDG_STATE_HOME`` or ``~/.local/state``)."""
    xdg = os.environ.get("XDG_STATE_HOME", "")
    if xdg and xdg.strip():
        return Path(xdg.strip()).expanduser()
    return Path.home() / ".local" / "state"


def config_home() -> Path:
    """Return the XDG config base (``$XDG_CONFIG_HOME`` or ``~/.config``)."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg and xdg.strip():
        return Path(xdg.strip()).expanduser()
    return Path.home() / ".config"


def cache_home() -> Path:
    """Return the XDG cache base (``$XDG_CACHE_HOME`` or ``~/.cache``)."""
    xdg = os.environ.get("XDG_CACHE_HOME", "")
    if xdg and xdg.strip():
        return Path(xdg.strip()).expanduser()
    return Path.home() / ".cache"


def saberops_state_dir() -> Path:
    """Return the canonical public state directory for the active profile."""
    return _profiled_app_dir(state_home(), STATE_APP_DIR)


def saberops_config_dir() -> Path:
    """Return the canonical public config directory for the active profile."""
    return _profiled_app_dir(config_home(), CONFIG_APP_DIR)


def saberops_cache_dir() -> Path:
    """Return the canonical public cache directory for the active profile."""
    return _profiled_app_dir(cache_home(), CACHE_APP_DIR)


def owner_state_dir() -> Path:
    """Return the global owner-wide state directory (canonical public root)."""
    return saberops_state_dir()


def projects_state_root() -> Path:
    """Return the parent directory holding every project-local state dir."""
    return owner_state_dir() / _PROJECTS_DIR


def get_default_db_path() -> Path:
    """Return the default global SQLite database path."""
    return saberops_state_dir() / "orchestrator.db"


def get_user_routing_path() -> Path:
    """Return the canonical owner routing-config path."""
    return saberops_config_dir() / "routing.json"


def get_authority_config_path() -> Path:
    """Return the canonical owner authority-config path."""
    return saberops_config_dir() / "authority.json"


def get_control_plane_config_path() -> Path:
    """Return the canonical owner control-plane-config path."""
    return saberops_config_dir() / "control_plane.json"


def get_binding_store_path_no_override() -> Path:
    """Return the canonical owner binding-document path (no env override)."""
    return saberops_config_dir() / "bindings.json"


def get_tool_registry_path() -> Path:
    """Return the canonical logical-tool registry path."""
    return saberops_config_dir() / "tools.json"


def get_projects_registry_path() -> Path:
    """Return the canonical UI project-registry path."""
    return saberops_state_dir() / "ui-projects.json"


def legacy_state_dirs() -> tuple[Path, ...]:
    """List legacy private-development state dirs (diagnostics only).

    These are never selected as defaults and never mutated; they exist
    so tests and diagnostics can prove the new namespace is independent.
    """
    base = state_home()
    return tuple(base / name for name in LEGACY_STATE_APP_DIRS)


def legacy_config_dirs() -> tuple[Path, ...]:
    """List legacy private-development config dirs (diagnostics only)."""
    base = config_home()
    return tuple(base / name for name in LEGACY_CONFIG_APP_DIRS)


def is_legacy_path(path: Path | str) -> bool:
    """Return True when ``path`` lives under a legacy private namespace."""
    text = str(path)
    return ("orchestrator-mvp" in text) or ("orchestrator-v2" in text)
