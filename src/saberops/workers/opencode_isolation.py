"""Private config/home isolation for every OpenCode CLI launch (HV-2).

``--pure``/``--dir`` were removed in Step 024 (HV-2) because the installed
``opencode v2.0.29`` help documents neither.  This module is the replacement
guard: it gives each OpenCode child a private, disposable config/home root and
disables project/ancestor configuration discovery, so no user, project or
ancestor configuration, plugin, MCP server or skill can be discovered or
executed by a SaberOps-launched ``opencode`` process.

Every lever below is taken from the installed binary's own embedded source
(read-only trace, cited with byte offsets in
``LANE/isolation-repair-proposal.md``):

* ``XDG_CONFIG_HOME`` re-points the global config directory
  (``<XDG_CONFIG_HOME>/opencode``); ``HOME`` re-points the ``~/.claude`` and
  ``~/.agents`` sources; ``XDG_CACHE_HOME``/``XDG_STATE_HOME`` keep incidental
  writes inside the same root.
* ``XDG_DATA_HOME`` is pinned to the *real* data directory, resolved from the
  projected child environment **before** ``HOME`` is redirected.  OpenCode
  resolves ``auth.json`` and its session database from ``XDG_DATA_HOME``, so
  authentication, resumable sessions and the selected subscription keep
  working.  No credential value is read, copied or exported: only the
  directory path is passed through.
* ``OPENCODE_DISABLE_PROJECT_CONFIG=1`` disables the ``cwd``-to-``/`` walk
  that collects ``opencode.json``/``opencode.jsonc`` and ``.opencode``
  directories, so untrusted worktree and ancestor content cannot load.
* Ambient configuration selectors are removed (see
  :data:`OPENCODE_AMBIENT_CONFIG_SELECTORS`).
* The inline config additionally carries deny policies for plugins, MCP
  servers and skills (defence in depth; the installed CLI documents
  ``policies`` as ordered top-level rules over configured resources).

The system config directory (``/etc/opencode`` and its platform equivalents)
has no environment switch, so a present system directory is a **refusal before
launch** (:class:`OpenCodeIsolationUnavailable`) rather than a silent bypass.
Callers surface that refusal through their existing typed failure forms.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "OPENCODE_AMBIENT_CONFIG_SELECTORS",
    "OPENCODE_DISCOVERY_DISABLE_ENV",
    "OpenCodeIsolation",
    "OpenCodeIsolationUnavailable",
    "opencode_isolation",
    "opencode_isolation_policies",
    "opencode_system_config_dir",
]

#: The exact owned env name that disables OpenCode project/ancestor discovery.
#: Never add an ``OPENCODE_*`` prefix to the hermetic allowlist; only this
#: exact name is forwarded.
OPENCODE_DISCOVERY_DISABLE_ENV = "OPENCODE_DISABLE_PROJECT_CONFIG"

#: Ambient env names that can select an OpenCode configuration, CLI
#: configuration or model-catalog source.  Every one of them is removed from
#: the child so a hostile or stale ambient value can never choose a config
#: file, a config directory, an inline document, the wellknown model catalog,
#: or the project-discovery switch itself.
OPENCODE_AMBIENT_CONFIG_SELECTORS: tuple[str, ...] = (
    "OPENCODE_CONFIG",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_CONFIG_CONTENT",
    "OPENCODE_CLI_CONFIG_CONTENT",
    "OPENCODE_CONFIG_PROJECT_DISABLE",
    "OPENCODE_DISABLE_PROJECT_CONFIG",
    "OPENCODE_MODELS_PATH",
    "OPENCODE_MODELS_URL",
    "OPENCODE_DISABLE_MODELS_FETCH",
)

#: Prefix of every root this module creates.  :meth:`OpenCodeIsolation.close`
#: refuses to remove anything that does not carry it.
_ROOT_PREFIX = "saberops-opencode-isolation-"

#: Sub-directories created inside an owned root.
_ROOT_DIRS: tuple[str, ...] = ("config", "home", "cache", "state")


class OpenCodeIsolationUnavailable(RuntimeError):
    """Raised before launch when OpenCode cannot be isolated.

    The message is a fixed, secret-free reason; callers surface it through
    their existing typed failure/unavailable forms and never launch the
    process.
    """


def opencode_system_config_dir(
    platform: str | None = None, environ: Mapping[str, str] | None = None
) -> Path:
    """Return the OpenCode system config directory for one platform.

    Mirrors the installed CLI's own resolution: ``darwin`` ->
    ``/Library/Application Support/opencode``; ``win32`` ->
    ``<ProgramData>\\opencode``; otherwise ``/etc/opencode``.
    """
    env = os.environ if environ is None else environ
    name = sys.platform if platform is None else platform
    if name == "darwin":
        return Path("/Library/Application Support/opencode")
    if name == "win32":
        return Path(env.get("ProgramData", "C:\\ProgramData")) / "opencode"
    return Path("/etc/opencode")


def opencode_isolation_policies() -> list[dict[str, str]]:
    """Return the deny policies pinned into an isolated inline config.

    ``policies`` is a top-level, ordered OpenCode config key; the resource
    matcher understands ``*``, so these deny every plugin, MCP server and
    skill regardless of the name a discovered source would add.
    """
    return [
        {"action": "integration.use", "resource": "plugin:*", "effect": "deny"},
        {"action": "integration.use", "resource": "mcp:*", "effect": "deny"},
        {"action": "integration.use", "resource": "skill:*", "effect": "deny"},
    ]


def _resolved_data_home(base_env: Mapping[str, str]) -> str:
    """Return the real data directory that holds auth.json and sessions.

    Resolved from the projected base environment, i.e. before ``HOME`` is
    redirected.  Only a path is derived; no credential file is read.
    """
    explicit = base_env.get("XDG_DATA_HOME")
    if explicit:
        return explicit
    home = base_env.get("HOME")
    if not home:
        raise OpenCodeIsolationUnavailable(
            "OpenCode isolation needs HOME or XDG_DATA_HOME to locate the "
            "existing account and session data directory"
        )
    return str(Path(home) / ".local" / "share")


@dataclass
class OpenCodeIsolation:
    """One owned OpenCode config/home root and its environment overrides."""

    root: Path
    overrides: dict[str, str]
    _closed: bool = field(default=False, repr=False)

    def config(
        self,
        base_config: Mapping[str, Any] | None = None,
        *,
        deny_integrations: bool = True,
    ) -> dict[str, Any]:
        """Return the inline OpenCode config for this child.

        ``deny_integrations`` adds the plugin/MCP/skill deny policies.  The
        shipped usage reader passes ``False`` because it deliberately loads
        exactly one SaberOps-owned plugin from this same inline config.
        """
        config: dict[str, Any] = dict(base_config or {})
        if deny_integrations:
            config["policies"] = opencode_isolation_policies()
        return config

    def env_for(
        self,
        base_env: Mapping[str, str],
        *,
        config: Mapping[str, Any] | None = None,
    ) -> dict[str, str]:
        """Return the final child environment.

        Ambient config selectors are removed, then this root's overrides are
        applied (pinned *after* the accepted overlay), then the inline config
        is written last so no ambient value can win.
        """
        env = {
            key: value
            for key, value in base_env.items()
            if key not in OPENCODE_AMBIENT_CONFIG_SELECTORS
        }
        env.update(self.overrides)
        if config is not None:
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
        return env

    def close(self) -> None:
        """Remove the owned root; a no-op when already closed.

        Only a directory this module created (its name carries
        :data:`_ROOT_PREFIX`) is ever removed, and never anything else.
        """
        if self._closed:
            return
        self._closed = True
        if self.root.name.startswith(_ROOT_PREFIX):
            shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> OpenCodeIsolation:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def opencode_isolation(
    base_env: Mapping[str, str],
    *,
    system_config_dir: Path | None = None,
) -> OpenCodeIsolation:
    """Create a private OpenCode config/home root for exactly one launch.

    ``base_env`` is the already-projected child environment (the accepted
    overlay); it is read only to resolve the real data directory before
    ``HOME`` is redirected.  A unique root per call keeps concurrent launches
    independent.

    Raises :class:`OpenCodeIsolationUnavailable` before any process launch
    when the system config directory exists (no supported switch can exclude
    it) or the account/session data directory cannot be resolved.
    """
    probe = opencode_system_config_dir() if system_config_dir is None else system_config_dir
    if probe.is_dir():
        raise OpenCodeIsolationUnavailable(
            "OpenCode system configuration is present and cannot be excluded; "
            "refusing to launch an OpenCode process"
        )
    data_home = _resolved_data_home(base_env)
    root = Path(tempfile.mkdtemp(prefix=_ROOT_PREFIX))
    for name in _ROOT_DIRS:
        (root / name).mkdir()
    overrides = {
        "HOME": str(root / "home"),
        "XDG_CONFIG_HOME": str(root / "config"),
        "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_STATE_HOME": str(root / "state"),
        "XDG_DATA_HOME": data_home,
        OPENCODE_DISCOVERY_DISABLE_ENV: "1",
    }
    return OpenCodeIsolation(root=root, overrides=overrides)
