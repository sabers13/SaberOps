"""Numbering and link integrity for the design records in ``docs/decisions``.

Parallel lanes chose "the next free number" independently, so local ``main``
once held two different records numbered 0013 and three numbered 0015.  A
duplicate number makes an ``ADR-00NN`` reference ambiguous, and a rename makes
every link to the old file dangle, so both are checked here rather than left to
review.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DECISIONS = REPO_ROOT / "docs" / "decisions"

_NUMBERED_FILE = re.compile(r"^(?P<number>\d{4})-[^/]+\.md$")
_TITLE = re.compile(r"^#\s+ADR-(?P<number>\d{4})\b")
_LINK = re.compile(r"\[[^\]]*\]\((?P<target>[^)\s]+)\)")


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
