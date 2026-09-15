"""Project registry managing recent and active Git project targets."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator_mvp.git import GitManager


def get_default_projects_path() -> Path:
    """Return default path for projects registry JSON."""
    xdg_state_home = os.environ.get("XDG_STATE_HOME")
    if xdg_state_home and xdg_state_home.strip():
        base_dir = Path(xdg_state_home).expanduser()
    else:
        base_dir = Path.home() / ".local" / "state"
    return base_dir / "orchestrator-mvp" / "ui-projects.json"


@dataclass(frozen=True)
class ProjectValidation:
    """Readiness validation outcome for a project directory."""

    path: str
    is_git: bool
    is_clean: bool
    ready: bool
    note: str


class ProjectRegistry:
    """Persists active and recent project paths in local JSON."""

    def __init__(self, path: Path | str | None = None) -> None:
        resolved = get_default_projects_path() if path is None else Path(path)
        self.path = resolved.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_file()

    def _ensure_file(self) -> None:
        if not self.path.exists():
            self._save({"active": None, "recent": []})

    def _load(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {"active": None, "recent": []}

    def _save(self, data: dict[str, Any]) -> None:
        temp_path = self.path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        temp_path.replace(self.path)

    def validate_project(self, path: Path | str) -> ProjectValidation:
        """Validate if target path is an existing git repo and clean."""
        p = Path(path).resolve()
        if not p.exists() or not p.is_dir():
            return ProjectValidation(
                path=str(p),
                is_git=False,
                is_clean=False,
                ready=False,
                note="Path does not exist or is not a directory",
            )
        if not GitManager.is_git_repo(p):
            return ProjectValidation(
                path=str(p),
                is_git=False,
                is_clean=False,
                ready=False,
                note="Not a valid Git repository",
            )
        clean = GitManager.is_clean(p)
        if not clean:
            return ProjectValidation(
                path=str(p),
                is_git=True,
                is_clean=False,
                ready=False,
                note="Repository has uncommitted or untracked changes",
            )
        return ProjectValidation(
            path=str(p),
            is_git=True,
            is_clean=True,
            ready=True,
            note="Clean Git repository ready for dispatch",
        )

    def list_projects(self) -> list[str]:
        """Return ordered recent project paths (unique, capped at 10)."""
        data = self._load()
        recent = data.get("recent", [])
        return [str(p) for p in recent if isinstance(p, str)]

    def active(self) -> Path | None:
        """Return the currently active project path if set, or fallback to first recent."""
        data = self._load()
        act = data.get("active")
        if act and isinstance(act, str):
            return Path(act).resolve()
        recent = self.list_projects()
        if recent:
            return Path(recent[0]).resolve()
        return None

    def add_project(self, path: Path | str) -> tuple[bool, str]:
        """Add project to recents if valid Git repo without necessarily changing active."""
        val = self.validate_project(path)
        if not val.is_git:
            return False, val.note
        p_str = str(Path(path).resolve())
        data = self._load()
        recent: list[str] = [p for p in data.get("recent", []) if p != p_str]
        recent.insert(0, p_str)
        recent = recent[:10]
        data["recent"] = recent
        if data.get("active") is None:
            data["active"] = p_str
        self._save(data)
        return True, val.note

    def select(self, path: Path | str) -> tuple[bool, str]:
        """Select project as active and update recents ordering."""
        val = self.validate_project(path)
        if not val.is_git:
            return False, val.note
        p_str = str(Path(path).resolve())
        data = self._load()
        recent: list[str] = [p for p in data.get("recent", []) if p != p_str]
        recent.insert(0, p_str)
        recent = recent[:10]
        data["recent"] = recent
        data["active"] = p_str
        self._save(data)
        return True, val.note
