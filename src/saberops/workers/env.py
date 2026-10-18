"""Hermetic child-environment construction for native CLI worker adapters.

Every native CLI worker child receives an explicit allowlist of environment
variables: non-secret runtime variables required to execute the CLI, the
per-harness configuration directory when applicable, and only the SaberOps-owned
subset of the overlay supplied in ``WorkerRequest.env``.  Nothing else from the
parent ``os.environ`` reaches the child.

The overlay is *not* small in production: both orchestrator dispatch sites build
``request.env`` as ``_filtered_worker_env({**os.environ, ...})`` (see
``orchestrator.py``), so it carries the whole parent environment.  This module
therefore filters the overlay through an allowlist as well; the per-harness
credential denylist is kept as a second layer where deny wins over allow.

This module is the single source of truth for that projection.  It is
deliberately independent of any adapter so the policy can be unit tested and
audited in one place.  Adapters pass the returned mapping to
``launch_worker_process`` together with ``inherit_env=False``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

__all__ = [
    "BASE_ENV_ALLOWLIST",
    "BASE_ENV_ALLOWLIST_PREFIXES",
    "HARNESS_ENV_ADDITIONS",
    "SABEROPS_OWNED_ENV_ALLOWLIST",
    "SABEROPS_OWNED_ENV_PREFIXES",
    "hermetic_env",
]

#: Exact non-secret runtime variable names every native CLI child may need.
#: ``LC_*`` is additionally matched by prefix (see
#: :data:`BASE_ENV_ALLOWLIST_PREFIXES`); every other entry is an exact name.
BASE_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "TZ",
    "TERM",
    "COLORTERM",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
    "XDG_STATE_HOME",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
)

#: Additional allowlist prefixes matched against the parent environment.
BASE_ENV_ALLOWLIST_PREFIXES: tuple[str, ...] = ("LC_",)

#: Per-harness additional variables projected from the parent environment.
#: These name the harness's own configuration directory, never a credential.
HARNESS_ENV_ADDITIONS: Mapping[str, tuple[str, ...]] = {
    "claude": ("CLAUDE_CONFIG_DIR",),
    "codex": ("CODEX_HOME",),
    "opencode": (),
    "antigravity": (),
    "cline": (),
}

#: Exact SaberOps-owned overlay names safe to forward to a native child.
#: ``CLAUDE_CODE_DISABLE_FAST_MODE`` is set by ``workers/claude.py`` so the
#: separately billed Fast mode can never be enabled for the selected
#: subscription connection.  The four ``OPENCODE_*`` names are the only
#: OpenCode values SaberOps itself sets: the server username/password and the
#: inline server configuration (see ``session_runtime.py``), plus the exact
#: discovery switch pinned by ``workers/opencode_isolation.py``.  Every other
#: ``OPENCODE_*`` name (``OPENCODE_CONFIG``, ``OPENCODE_CONFIG_DIR``,
#: ``OPENCODE_MODEL``, ...) is dropped so ambient shell configuration can
#: never change the provider/billing or the discovered configuration of a
#: worker child; the OpenCode adapters overwrite ``OPENCODE_CONFIG_CONTENT``
#: and the discovery switch with their own pinned values after building the
#: environment.
SABEROPS_OWNED_ENV_ALLOWLIST: tuple[str, ...] = (
    "CLAUDE_CODE_DISABLE_FAST_MODE",
    "OPENCODE_SERVER_PASSWORD",
    "OPENCODE_SERVER_USERNAME",
    "OPENCODE_CONFIG_CONTENT",
    "OPENCODE_DISABLE_PROJECT_CONFIG",
)

#: SaberOps-owned overlay prefixes safe to forward to a native child.  The
#: sources of these keys are:
#: * ``ORCH_`` -- orchestrator dispatch identity/tool keys
#:   (``orchestrator.py``), executable-binding identity and profile selectors
#:   (``control_plane/bindings.py``), the ``ORCH_MODEL_*`` / ``ORCH_BINDING_*``
#:   reason vocabulary, and adapter profile selectors
#:   (``workers/opencode.py``, ``workers/antigravity.py``, ``workers/cline.py``);
#: * ``_ORCH_`` -- the Copilot MiniMax sentinel (``workers/copilot.py``).
#:
#: The removed gateway seam formerly emitted ``ORCH_GATEWAY_*`` control
#: names under the ``ORCH_`` prefix and a loopback bridge sentinel under
#: ``_ORCH_``; those names are now denied for every harness (see
#: :func:`_overlay_denied`), so stale values can never reach a child.
#:
#: There is deliberately no ``OPENCODE_`` prefix entry: only the three
#: exact ``OPENCODE_*`` names in :data:`SABEROPS_OWNED_ENV_ALLOWLIST` are
#: forwarded, so ambient shell configuration (custom config paths, model
#: selectors, inline config) can never reach a worker child.
SABEROPS_OWNED_ENV_PREFIXES: tuple[str, ...] = ("ORCH_", "_ORCH_")

#: The removed loopback bridge's authorization sentinel.  Denied for every
#: harness: the gateway cut removed the bridge, so this name must never
#: travel in a child environment again.
_BRIDGE_AUTH_SENTINEL_ENV = "_ORCH_BRIDGE_AUTH_SENTINEL"

#: Gateway-control prefix from the removed gateway seam.  Denied for every
#: native harness even when present in the overlay: ``ORCH_GATEWAY_*`` is
#: simply not allowed for any harness.
_GATEWAY_ENV_PREFIX = "ORCH_GATEWAY_"

#: Existing credential denylists applied to the ``request.env`` overlay as
#: well, so the hermetic projection is never weaker than the pre-existing
#: adapter behaviour.  Matching is by exact name (``_NAMES``) or prefix
#: (``_PREFIXES``) and deny always wins over allow.
_OVERLAY_DENYLIST_PREFIXES: Mapping[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_", "CLAUDE_CODE_USE_"),
}
_OVERLAY_DENYLIST_NAMES: Mapping[str, frozenset[str]] = {
    "antigravity": frozenset({"GEMINI_API_KEY", "GOOGLE_API_KEY"}),
}


def _overlay_allowed(harness: str, key: str) -> bool:
    """Return True when an overlay ``key`` may be forwarded to ``harness``."""
    if key in BASE_ENV_ALLOWLIST:
        return True
    if key.startswith(BASE_ENV_ALLOWLIST_PREFIXES):
        return True
    if key in HARNESS_ENV_ADDITIONS.get(harness, ()):
        return True
    if key in SABEROPS_OWNED_ENV_ALLOWLIST:
        return True
    return key.startswith(SABEROPS_OWNED_ENV_PREFIXES)


def _overlay_denied(harness: str, key: str) -> bool:
    """Return True when ``key`` is denied for ``harness`` (deny wins)."""
    if key == _BRIDGE_AUTH_SENTINEL_ENV:
        return True
    if key.startswith(_GATEWAY_ENV_PREFIX):
        return True
    if any(
        key.startswith(prefix)
        for prefix in _OVERLAY_DENYLIST_PREFIXES.get(harness, ())
    ):
        return True
    return key in _OVERLAY_DENYLIST_NAMES.get(harness, frozenset())


def hermetic_env(
    harness: str,
    overlay: Mapping[str, str] | None,
    parent_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the hermetic child environment for one native CLI dispatch.

    ``harness`` is the adapter identity (``claude``, ``codex``, ``opencode``,
    ``antigravity``, or ``cline``).  ``overlay`` is the environment SaberOps
    supplied in ``WorkerRequest.env``; only its allowlisted subset is applied.
    ``parent_env`` defaults to ``os.environ``.

    Only these keys reach the returned mapping:

    * exact base-allowlist names (:data:`BASE_ENV_ALLOWLIST`);
    * names with an allowed prefix (:data:`BASE_ENV_ALLOWLIST_PREFIXES`,
      i.e. ``LC_*``);
    * the ``harness``-specific additions (:data:`HARNESS_ENV_ADDITIONS`);
    * overlay keys that are themselves allowlisted runtime names or
      SaberOps-owned names/prefixes (:data:`SABEROPS_OWNED_ENV_ALLOWLIST`,
      :data:`SABEROPS_OWNED_ENV_PREFIXES`).

    The harness credential denylist and the gateway-control denylist are
    applied last and win over the allowlist.  The function never mutates
    ``os.environ``.
    """
    source: Mapping[str, str] = os.environ if parent_env is None else parent_env
    child: dict[str, str] = {}
    for key in BASE_ENV_ALLOWLIST:
        value = source.get(key)
        if value is not None:
            child[key] = value
    for key, value in source.items():
        if key.startswith(BASE_ENV_ALLOWLIST_PREFIXES):
            child[key] = value
    for key in HARNESS_ENV_ADDITIONS.get(harness, ()):
        value = source.get(key)
        if value is not None:
            child[key] = value
    if overlay:
        for key, value in overlay.items():
            if not _overlay_allowed(harness, key):
                continue
            if _overlay_denied(harness, key):
                continue
            child[key] = value
    return child
