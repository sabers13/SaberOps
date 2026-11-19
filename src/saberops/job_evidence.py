"""Plain per-job evidence folders and per-project worker notes.

Two durable, human-readable artefacts live beside the project database:

``<project state dir>/project-notes.md``
    A short note the owner maintains per project.  It is handed to every
    worker attempt in a ``[PROJECT NOTES]`` section.

``<project state dir>/jobs/<run_id>/``
    One plain folder per run ("job"): the exact request, each attempt's exact
    prompt, the project note that attempt actually received, the worker
    result, and a small JSON manifest linking the attempt to the existing
    dispatch report's requested and provider-reported model fields.

The folder is append-only evidence.  Edits to ``project-notes.md`` never
rewrite an earlier attempt's snapshot: each attempt keeps the bytes it was
sent, plus that snapshot's SHA-256 digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Final

PROJECT_NOTES_FILENAME: Final[str] = "project-notes.md"
JOBS_DIRNAME: Final[str] = "jobs"
SCHEMA_VERSION: Final[int] = 1
UNREPORTED: Final[str] = "unreported"
_MAX_RESULT_OUTPUT_CHARS: Final[int] = 20_000
_HEADER_RE: Final = re.compile(r"(?m)^\[[^\]\n]+\]$")


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    fd: int | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
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


def project_notes_path(state_dir: Path | str) -> Path:
    """Return the owner-maintained per-project notes file path."""
    return Path(state_dir) / PROJECT_NOTES_FILENAME


def normalize_project_notes(text: str | None) -> str | None:
    """Return the exact note body sent to workers, or ``None`` when empty."""
    if text is None:
        return None
    stripped = text.strip()
    return stripped or None


def project_notes_digest(text: str) -> str:
    """Return the stable SHA-256 digest of the exact note body."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_project_notes(state_dir: Path | str) -> str | None:
    """Read the project note from ``state_dir``.

    A missing file means "no note".  An unreadable file fails closed with
    :class:`OSError` so an attempt never silently drops the owner's note.
    """
    path = project_notes_path(state_dir)
    if not path.is_file():
        return None
    return normalize_project_notes(path.read_text(encoding="utf-8"))


def write_project_notes(state_dir: Path | str, text: str | None) -> Path:
    """Replace the project note atomically (``None`` clears it)."""
    path = project_notes_path(state_dir)
    body = normalize_project_notes(text)
    if body is None:
        path.unlink(missing_ok=True)
        return path
    _atomic_write_text(path, body + "\n")
    return path


def job_folder(state_dir: Path | str, run_id: str) -> Path:
    """Return the plain per-run folder for ``run_id``."""
    return Path(state_dir) / JOBS_DIRNAME / run_id


def _attempt_stem(attempt_number: int) -> str:
    return f"attempt-{attempt_number}"


def _prompt_name(attempt_number: int) -> str:
    return f"{_attempt_stem(attempt_number)}.prompt.md"


def _note_name(attempt_number: int) -> str:
    return f"{_attempt_stem(attempt_number)}.note.md"


def _result_name(attempt_number: int) -> str:
    return f"{_attempt_stem(attempt_number)}.result.md"


def _manifest_name(attempt_number: int) -> str:
    return f"{_attempt_stem(attempt_number)}.json"


def record_request(
    folder: Path,
    *,
    run_id: str,
    task: str,
    target_repo: str,
    base_commit: str,
    created_at: str,
) -> Path:
    """Write ``request.md``: what was asked, in plain text."""
    path = folder / "request.md"
    body = (
        "# Request\n"
        "\n"
        f"- run: {run_id}\n"
        f"- repository: {target_repo}\n"
        f"- base commit: {base_commit}\n"
        f"- created: {created_at}\n"
        "\n"
        "## Task\n"
        "\n"
        f"{task.rstrip()}\n"
    )
    _atomic_write_text(path, body)
    return path


def _prompt_note_section(prompt: str) -> str | None:
    """Return the exact ``[PROJECT NOTES]`` body carried by ``prompt``.

    The snapshot is taken from the prompt itself, so the saved note can never
    disagree with what the worker received, even if the owner edits the live
    notes file while an attempt is being prepared.
    """
    marker = "\n[PROJECT NOTES]\n"
    index = prompt.find(marker)
    if index < 0:
        return None
    body = prompt[index + len(marker):]
    next_header = next(
        (match.start() for match in _HEADER_RE.finditer(body)),
        None,
    )
    if next_header is not None:
        body = body[:next_header]
    return normalize_project_notes(body)


def record_attempt_prompt(
    folder: Path,
    *,
    run_id: str,
    attempt_number: int,
    prompt: str,
    dispatch_evidence_id: str,
    requested_provider: str,
    requested_model: str,
    project_note: str | None,
) -> dict[str, Any]:
    """Persist one attempt's exact prompt and the exact project note sent.

    The note snapshot is read from the prompt's own ``[PROJECT NOTES]``
    section, so the snapshot and the captured prompt can never disagree.  The
    manifest starts as ``started`` and is completed by
    :func:`record_attempt_result`.
    """
    body = _prompt_note_section(prompt) or normalize_project_notes(project_note)
    prompt_path = folder / _prompt_name(attempt_number)
    _atomic_write_text(prompt_path, prompt)

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "attempt": attempt_number,
        "dispatch_evidence_id": dispatch_evidence_id,
        "requested_provider": requested_provider,
        "requested_model": requested_model,
        "reported_provider": None,
        "reported_model": None,
        "identity_status": "pending",
        "prompt_file": prompt_path.name,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "status": "started",
    }
    if body is not None:
        note_path = folder / _note_name(attempt_number)
        _atomic_write_text(note_path, body)
        manifest.update(
            {
                "project_note_file": note_path.name,
                "project_note_sha256": project_notes_digest(body),
                "project_note_chars": len(body),
            }
        )
    _write_manifest(folder, attempt_number, manifest)
    return manifest


def record_attempt_result(
    folder: Path,
    *,
    attempt_number: int,
    status: str,
    actual_provider: str | None,
    actual_model: str | None,
    commit_sha: str | None,
    summary: str,
    stdout: str = "",
    stderr: str = "",
) -> dict[str, Any]:
    """Complete one attempt's manifest and write ``attempt-N.result.md``.

    A provider that supplied no identity is recorded as ``unreported`` rather
    than a guessed or empty value.
    """
    manifest = _read_manifest(folder, attempt_number)
    identity_reported = bool(actual_model) and actual_model != UNREPORTED
    manifest.update(
        {
            "identity_status": "reported" if identity_reported else UNREPORTED,
            "reported_provider": actual_provider if actual_provider is not None else UNREPORTED,
            "reported_model": actual_model if identity_reported else UNREPORTED,
            "status": status,
            "commit_sha": commit_sha,
        }
    )
    _write_manifest(folder, attempt_number, manifest)

    result_path = folder / _result_name(attempt_number)
    lines = [
        f"# Attempt {attempt_number} result",
        "",
        f"- status: {status}",
        f"- commit: {commit_sha or '(none)'}",
        f"- requested: {manifest.get('requested_provider')}/{manifest.get('requested_model')}",
        f"- provider-reported: {manifest['reported_provider']}/{manifest['reported_model']}",
        f"- identity: {manifest['identity_status']}",
        "",
        "## Summary",
        "",
        (summary or "(none)").rstrip(),
    ]
    out = stdout.strip()
    err = stderr.strip()
    if out:
        lines += ["", "## stdout", "", out[:_MAX_RESULT_OUTPUT_CHARS]]
    if err:
        lines += ["", "## stderr", "", err[:_MAX_RESULT_OUTPUT_CHARS]]
    _atomic_write_text(result_path, "\n".join(lines) + "\n")
    manifest["result_file"] = result_path.name
    _write_manifest(folder, attempt_number, manifest)
    return manifest


def _write_manifest(folder: Path, attempt_number: int, manifest: dict[str, Any]) -> None:
    _atomic_write_text(
        folder / _manifest_name(attempt_number),
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )


def _read_manifest(folder: Path, attempt_number: int) -> dict[str, Any]:
    path = folder / _manifest_name(attempt_number)
    if not path.is_file():
        return {
            "schema_version": SCHEMA_VERSION,
            "attempt": attempt_number,
        }
    try:
        decoded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": SCHEMA_VERSION, "attempt": attempt_number}
    return decoded if isinstance(decoded, dict) else {"schema_version": SCHEMA_VERSION}


def read_attempt_manifest(folder: Path, attempt_number: int) -> dict[str, Any]:
    """Read one attempt manifest (tests and read-only inspection)."""
    return _read_manifest(folder, attempt_number)


__all__ = [
    "JOBS_DIRNAME",
    "PROJECT_NOTES_FILENAME",
    "UNREPORTED",
    "job_folder",
    "normalize_project_notes",
    "project_notes_digest",
    "project_notes_path",
    "read_attempt_manifest",
    "read_project_notes",
    "record_attempt_prompt",
    "record_attempt_result",
    "record_request",
    "write_project_notes",
]
