"""Local, durable owner uploads; file content is reference data, never authority."""

from __future__ import annotations

import mimetypes
import re
import shutil
import sqlite3
import subprocess
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 5
TEXT_LIMIT = 12000
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
TEXT_TYPES = {
    ".txt",
    ".md",
    ".json",
    ".csv",
    ".tsv",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".css",
    ".yaml",
    ".yml",
    ".toml",
    ".xml",
    ".log",
    ".sql",
    ".sh",
    ".c",
    ".cpp",
    ".h",
    ".rs",
    ".go",
    ".java",
    ".ini",
}


def parse_ids(raw: str) -> tuple[str, ...]:
    ids = tuple(dict.fromkeys(raw.split(","))) if raw else ()
    if len(ids) > MAX_FILES or any(not re.fullmatch(r"file_[a-f0-9]{32}", item) for item in ids):
        raise ValueError("Attach up to five files per message.")
    return ids


class AttachmentStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.root = db_path.parent / "chat-attachments"
        with sqlite3.connect(db_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS chat_attachments (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL,
                stored_name TEXT NOT NULL, created REAL NOT NULL, chat_id TEXT, request_id TEXT)""")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS attachment_turn ON chat_attachments(chat_id,request_id)"
            )

    def save(self, filename: str, content: bytes) -> dict[str, Any]:
        name = re.sub(r"[\x00-\x1f\x7f]", "", filename.replace("\\", "/").split("/")[-1])[:160]
        suffix = Path(name).suffix.lower()
        if not name or not content or len(content) > MAX_FILE_BYTES:
            raise ValueError("Each file must be nonempty and no larger than 5 MB.")
        if suffix not in IMAGE_TYPES and suffix not in TEXT_TYPES | {".pdf", ".docx"}:
            raise ValueError("Choose an image, PDF, Word document, text or code file.")
        mime = IMAGE_TYPES.get(suffix) or mimetypes.guess_type(name)[0] or "text/plain"
        if suffix in IMAGE_TYPES:
            valid = (
                content.startswith(b"\x89PNG\r\n\x1a\n")
                if suffix == ".png"
                else content.startswith(b"\xff\xd8\xff")
                if suffix in (".jpg", ".jpeg")
                else content[:6] in (b"GIF87a", b"GIF89a")
                if suffix == ".gif"
                else content.startswith(b"RIFF") and content[8:12] == b"WEBP"
            )
            if not valid:
                raise ValueError("The image does not match its file type.")
        if suffix == ".pdf" and not content.startswith(b"%PDF-"):
            raise ValueError("Choose a valid PDF file.")
        if suffix in TEXT_TYPES:
            try:
                content.decode("utf-8-sig")
            except UnicodeDecodeError as exc:
                raise ValueError("Text and code attachments must use UTF-8.") from exc
        identity = "file_" + uuid.uuid4().hex
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.root / (identity + suffix)
        try:
            with path.open("xb") as stream:
                stream.write(content)
            path.chmod(0o600)
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    "INSERT INTO chat_attachments VALUES (?,?,?,?,?,?,NULL,NULL)",
                    (identity, name, mime, len(content), path.name, time.time()),
                )
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return self.get(identity) or {}

    @staticmethod
    def public(row: sqlite3.Row) -> dict[str, Any]:
        return {key: row[key] for key in ("id", "name", "mime", "size")} | {
            "url": f"/chat/attachments/{row['id']}"
        }

    def get(self, identity: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM chat_attachments WHERE id=?", (identity,)).fetchone()
            return self.public(row) if row else None

    def path(self, identity: str) -> Path:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT stored_name FROM chat_attachments WHERE id=?", (identity,)
            ).fetchone()
        if not row:
            raise ValueError("Attachment not found.")
        path = (self.root / str(row[0])).resolve()
        if not path.is_relative_to(self.root.resolve()) or not path.is_file():
            raise ValueError("Attachment not found.")
        return path

    def remove(self, identity: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT stored_name,chat_id FROM chat_attachments WHERE id=?", (identity,)
            ).fetchone()
            if not row or row[1]:
                return False
            path = self.root / row[0]
            conn.execute("DELETE FROM chat_attachments WHERE id=?", (identity,))
            path.unlink(missing_ok=True)
            return True

    def bind(
        self, conn: sqlite3.Connection, ids: tuple[str, ...], chat_id: str, request: str
    ) -> None:
        if len(ids) > MAX_FILES:
            raise ValueError("Attach up to five files per message.")
        for identity in ids:
            row = conn.execute(
                "SELECT chat_id,request_id FROM chat_attachments WHERE id=?", (identity,)
            ).fetchone()
            if not row or (row[0] and (row[0], row[1]) != (chat_id, request)):
                raise ValueError("An attachment is missing or belongs to another message.")
        for identity in ids:
            conn.execute(
                "UPDATE chat_attachments SET chat_id=?,request_id=? WHERE id=?",
                (chat_id, request, identity),
            )

    def for_message(
        self, conn: sqlite3.Connection, chat: str, request: str | None
    ) -> list[dict[str, Any]]:
        return [
            self.public(row)
            for row in conn.execute(
                "SELECT * FROM chat_attachments WHERE chat_id=? AND request_id=? "
                "ORDER BY created,id",
                (chat, request),
            )
        ]

    def text(self, identity: str) -> str:
        path = self.path(identity)
        try:
            if path.suffix == ".pdf":
                result = subprocess.run(
                    ["pdftotext", "-f", "1", "-l", "30", str(path), "-"],
                    capture_output=True,
                    timeout=8,
                    check=True,
                )
                return result.stdout.decode("utf-8", errors="replace")[:TEXT_LIMIT]
            if path.suffix == ".docx":
                with zipfile.ZipFile(path) as archive:
                    member = archive.getinfo("word/document.xml")
                    if member.file_size > 2_000_000:
                        return "Document text is too large to preview."
                    root = ElementTree.fromstring(archive.read(member))
                    return " ".join(
                        node.text or "" for node in root.iter() if node.tag.endswith("}t")
                    )[:TEXT_LIMIT]
            if path.suffix in TEXT_TYPES:
                return path.read_text(encoding="utf-8-sig")[:TEXT_LIMIT]
        except (
            OSError,
            ValueError,
            subprocess.SubprocessError,
            zipfile.BadZipFile,
            KeyError,
            ElementTree.ParseError,
        ):
            return "Text extraction unavailable; the original file is retained."
        return ""

    def prepare(
        self, messages: list[dict[str, Any]], scratch: Path
    ) -> tuple[list[dict[str, Any]], tuple[Path, ...]]:
        files = [item for message in messages for item in message.get("attachments", [])][-20:]
        images = [item["id"] for item in files if item["mime"].startswith("image/")][-MAX_FILES:]
        result = []
        image_paths = []
        for item in files:
            source = self.path(item["id"])
            target = scratch / source.name
            shutil.copyfile(source, target)
            result.append(
                dict(item, path=str(target), text=self.text(item["id"]), reference_only=True)
            )
            if item["id"] in images:
                image_paths.append(target)
        return result, tuple(image_paths)
