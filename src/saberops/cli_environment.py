"""Discover conventional CLI installations without executing shell startup files."""

from __future__ import annotations

import os
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
