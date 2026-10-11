"""Discover conventional CLI installations without executing shell startup files."""

from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping
from pathlib import Path


def prepare_cli_environment(
    *, home: Path | None = None, environ: MutableMapping[str, str] | None = None
) -> None:
    """Append existing user installation directories to the process PATH.

    Desktop/service launches often inherit a smaller PATH than a terminal.
    Keeping discovery and child processes on the same PATH ensures that an
    installed CLI (and its Node runtime) can also be launched by its adapter.
    Explicit PATH choices retain priority. No login files or credentials are read.
    """
    env = os.environ if environ is None else environ
    root = Path.home() if home is None else home
    candidates = [
        root / ".local/bin",
        root / ".opencode/bin",
        root / ".bun/bin",
        root / ".npm-global/bin",
        root / ".volta/bin",
        root / ".asdf/shims",
        root / ".local/share/mise/shims",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
    ]
    for key in ("NVM_BIN", "FNM_MULTISHELL_PATH"):
        value = env.get(key, "")
        if value:
            candidates.append(Path(value).expanduser())
    nvm = Path(env.get("NVM_DIR") or str(root / ".nvm")).expanduser()
    versions = nvm / "versions/node"

    def version_key(path: Path) -> tuple[int, ...]:
        parts = path.name.removeprefix("v").split(".")
        return tuple(int(part) if part.isdigit() else 0 for part in parts)

    if versions.is_dir():
        candidates.extend(
            version / "bin"
            for version in sorted(versions.iterdir(), key=version_key, reverse=True)
            if (version / "bin/node").is_file()
        )
    appdata = env.get("APPDATA", "")
    if appdata:
        candidates.append(Path(appdata) / "npm")
    paths = env.get("PATH", os.defpath).split(os.pathsep)
    seen = {os.path.normcase(os.path.abspath(path)) for path in paths}
    for directory in candidates:
        key = os.path.normcase(os.path.abspath(directory))
        if key not in seen and directory.is_dir():
            paths.append(str(directory))
            seen.add(key)
    env["PATH"] = os.pathsep.join(paths)


def resolve_owner_argv_prefix() -> list[str]:
    """Resolve the argv prefix for a detached/supervised execution owner.

    Single authority for execution-owner identity.  By default the
    continuation re-executes the **same** Python environment/code identity
    that launched it via ``[sys.executable, "-m", "saberops.cli"]`` -- this
    covers an installed venv, an editable install, and a source/dev
    execution whose checkout arrives via the inherited ``PYTHONPATH``
    (see ``RunSupervisor._spawn_owner``).  No ``orch`` entry point is
    discovered from ``$PATH`` on the default path, so a stale global
    ``orch`` can never become the continuation authority.

    ``ORCH_EXECUTABLE`` remains an explicit owner/developer override and,
    when set, yields the single-element prefix ``[override]``.
    """
    override = os.environ.get("ORCH_EXECUTABLE", "").strip()
    if override:
        return [override]
    return [sys.executable, "-m", "saberops.cli"]


def resolve_orch_executable() -> str:
    """Resolve the orchestrator executable identity for display/diagnostics.

    Thin display wrapper over :func:`resolve_owner_argv_prefix` (the single
    launch authority): the override when set, else the current interpreter
    identity.  Never falls back to ``shutil.which("orch")`` or any other
    ``$PATH`` discovery, so diagnostics cannot mask a stale global ``orch``.
    """
    return " ".join(resolve_owner_argv_prefix())
