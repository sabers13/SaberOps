"""Numbering and link integrity for the design records in ``docs/decisions``.

Parallel lanes chose "the next free number" independently, so local ``main``
once held two different records numbered 0013 and three numbered 0015.  A
duplicate number makes an ``ADR-00NN`` reference ambiguous, and a rename makes
every link to the old file dangle, so both are checked here rather than left to
review.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DECISIONS = REPO_ROOT / "docs" / "decisions"

_NUMBERED_FILE = re.compile(r"^(?P<number>\d{4})-[^/]+\.md$")
_TITLE = re.compile(r"^#\s+ADR-(?P<number>\d{4})\b")
_LINK = re.compile(r"\[[^\]]*\]\((?P<target>[^)\s]+)\)")

# Records that were never written and are deleted rather than reconstructed.
# The identifiers are assembled so this module does not cite them itself.
_RETIRED_ANCHORS = tuple("0" * 3 + digit for digit in "234")
_RETIRED_FILE_STEMS = tuple(
    f"{number}-{slug}"
    for number, slug in zip(
        _RETIRED_ANCHORS,
        (
            "routing-admission-and-quota",
            "gate-capacity-and-storage",
            "mcp-authority-and-evidence",
        ),
        strict=True,
    )
)
_RETIRED_CITATION = re.compile(
    r"(?<![A-Za-z0-9])ADR-(?:" + "|".join(_RETIRED_ANCHORS) + r")(?![0-9])"
)
_RETIRED_FILE_REFERENCE = re.compile("|".join(re.escape(stem) for stem in _RETIRED_FILE_STEMS))
_SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "build",
        "dist",
        ".trial",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)


def _cited_documents() -> Iterator[Path]:
    """Yield every Markdown or Python document that could carry a citation."""
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file() or path.suffix not in {".py", ".md"}:
            continue
        parts = path.relative_to(REPO_ROOT).parts
        if _SKIPPED_DIRECTORIES.intersection(parts):
            continue
        yield path


def _decision_files() -> list[Path]:
    return sorted(DECISIONS.glob("*.md"))


def _number(path: Path) -> str:
    match = _NUMBERED_FILE.match(path.name)
    assert match is not None, f"decision record is not numbered: {path.name}"
    return match.group("number")


def test_decision_numbers_are_unique() -> None:
    seen: dict[str, Path] = {}
    duplicates: list[str] = []
    for path in _decision_files():
        number = _number(path)
        if number in seen:
            duplicates.append(f"{number}: {seen[number].name} and {path.name}")
        else:
            seen[number] = path
    assert not duplicates, "decision records share a number: " + "; ".join(duplicates)


def test_decision_titles_match_their_file_numbers() -> None:
    mismatches: list[str] = []
    for path in _decision_files():
        first_line = path.read_text(encoding="utf-8").splitlines()[0]
        match = _TITLE.match(first_line)
        if match is None or match.group("number") != _number(path):
            mismatches.append(f"{path.name}: {first_line!r}")
    assert not mismatches, "decision title ADR numbers do not match filenames: " + "; ".join(
        mismatches
    )


def test_decision_links_point_at_existing_files() -> None:
    documents = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").rglob("*.md"))]
    broken: list[str] = []
    for document in documents:
        text = document.read_text(encoding="utf-8")
        for match in _LINK.finditer(text):
            target = match.group("target").split("#", 1)[0].split("?", 1)[0]
            if "://" in target or target.startswith("mailto:") or not target.endswith(".md"):
                continue
            resolved = (document.parent / target).resolve()
            is_decision_link = resolved.parent == DECISIONS.resolve() or "decisions/" in target
            if is_decision_link and not resolved.is_file():
                broken.append(f"{document.relative_to(REPO_ROOT)}: {target}")
    assert not broken, "links to missing decision records: " + "; ".join(broken)


def test_retired_records_are_not_cited() -> None:
    citations: list[str] = []
    for document in _cited_documents():
        for number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), 1):
            if _RETIRED_CITATION.search(line) or _RETIRED_FILE_REFERENCE.search(line):
                citations.append(f"{document.relative_to(REPO_ROOT)}:{number}")
    assert not citations, "files cite a deleted decision record: " + ", ".join(citations)
