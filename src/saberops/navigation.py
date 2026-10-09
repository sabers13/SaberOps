"""Read-only, bounded project folders for chat and run navigation."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from saberops.chat import ChatStore
from saberops.project import read_project_marker
from saberops.run_titles import fallback_title
from saberops.runtime import ProjectRuntimeManager


def project_folders(
    manager: ProjectRuntimeManager, store: ChatStore, paths: list[str], active: str
) -> list[dict[str, Any]]:
    """Read labels only; do not build runtimes, inspect lifecycle, or switch projects."""
    folders: dict[str, dict[str, Any]] = {}

    def folder(repo: str) -> dict[str, Any]:
        key = str(Path(repo).resolve()) if repo else ""
        return folders.setdefault(
            key,
            {
                "path": key,
                "name": Path(key).name if key else "Unassigned",
                "active": key == active,
                "entries": [],
            },
        )

    for repo_path in paths:
        folder(repo_path)
    databases = {manager.owner_db.db_path}
    if manager.pinned_db_path is None:
        for entry in sorted(manager.state_root.glob("*/project.json")):
            try:
                identity = read_project_marker(entry.parent)
                if identity is not None:
                    folder(identity.canonical_path)
                    databases.add(entry.parent / "orchestrator.db")
            except Exception:
                continue
    seen: set[str] = set()
    for path in sorted(databases):
        if not path.is_file():
            continue
        try:
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
                title_column = (
                    "display_title" if "display_title" in columns else "NULL AS display_title"
                )
                runs = conn.execute(f"""
                    SELECT id, task, target_repo, status, created_at, display_title FROM (
                        SELECT id, task, target_repo, status, created_at, {title_column},
                            ROW_NUMBER() OVER (
                                PARTITION BY target_repo ORDER BY created_at DESC
                            ) AS position
                        FROM runs
                    ) WHERE position <= 50 ORDER BY created_at DESC
                """).fetchall()
        except sqlite3.Error:
            continue
        for run in runs:
            if run["id"] in seen:
                continue
            seen.add(run["id"])
            try:
                created = datetime.fromisoformat(run["created_at"]).timestamp()
            except ValueError:
                created = 0.0
            folder(run["target_repo"])["entries"].append(
                {
                    "id": run["id"],
                    "kind": "run",
                    "title": run["display_title"]
                    or fallback_title(run["task"], run["target_repo"]),
                    "href": f"/runs/{run['id']}?from=runs#work",
                    "status": run["status"],
                    "created": created,
                }
            )
    with store.connect() as conn:
        # Bound per repository, rather than allowing one busy project to hide others.
        rows = conn.execute("""
            SELECT id, repo, title, created, kind FROM (
                SELECT c.id, c.repo, c.title, c.created, COALESCE(t.kind,'legacy') AS kind,
                    ROW_NUMBER() OVER (PARTITION BY c.repo ORDER BY c.created DESC) AS position
                FROM conversations c LEFT JOIN conversation_threads t ON t.chat_id=c.id
                WHERE NOT EXISTS (SELECT 1 FROM chat_runs r WHERE r.chat_id=c.id
                                  AND t.kind='work')
            ) WHERE position <= 20 ORDER BY created DESC
        """)
        for row in rows:
            folder(str(row["repo"]))["entries"].append(
                {
                    "id": row["id"],
                    "kind": "work" if row["kind"] == "work" else "chat",
                    "title": str(row["title"]),
                    "href": f"/chats/{row['id']}#" + ("work" if row["kind"] == "work" else "chat"),
                    "status": "Work" if row["kind"] == "work" else "Chat",
                    "created": row["created"],
                }
            )
    result = list(folders.values())
    for group in result:
        if sum(other["name"] == group["name"] for other in result) > 1:
            group["label"] = group["name"] + " · " + str(Path(group["path"]).parent)
        else:
            group["label"] = group["name"]
        group["entries"].sort(key=lambda item: item["created"], reverse=True)
        group["chats"] = [item for item in group["entries"] if item["kind"] == "chat"]
        group["work"] = [item for item in group["entries"] if item["kind"] != "chat"]
    result.sort(
        key=lambda group: (
            not group["active"],
            -max((item["created"] for item in group["entries"]), default=0),
            group["path"],
        )
    )
    return result
