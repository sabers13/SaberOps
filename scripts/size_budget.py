#!/usr/bin/env python3
"""Size check: first-party logic lines and suppression comments stay recorded.

Logic lines count Python code, not prose. A source file is stripped of its
strings, docstrings and comments (leaving their line breaks in place), and every
remaining non-blank line is a logic line. Plain comments therefore have no
limit. Suppression comments count check-disabling directives such as ``# noqa``
and ``# type: ignore``; prose that merely mentions one is not a directive.

The post-phase-1.1 numbers live in ``scripts/size_budget.json``. A change that
grows either count past its recorded number fails until the recorded number is
updated with a one-line reason. The budget under ``scripts/`` is a protected
gate definition, so raising the budget is an owner question on the real check path.

Usage:
    python scripts/size_budget.py --check
    python scripts/size_budget.py --write --reason "one line saying why"

Exit codes: 0 within the recorded budget, 1 growth past it, 2 a usage,
configuration or unreadable-source error (fail closed).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

#: First-party Python that the budget covers.
SOURCE_ROOTS: tuple[str, ...] = ("src", "tests", "scripts")

#: A comment that disables or narrows a check. Anchored at the comment start so
#: prose mentioning a directive ("# a comment about noqa") is not counted.
SUPPRESSION_COMMENT = re.compile(
    r"^#\s*(?:"
    r"noqa\b"
    r"|type:\s*ignore\b"
    r"|pyright:\s*ignore\b"
    r"|pragma:\s*no\s*cover\b"
    r"|nosec\b"
    r"|pylint:\s*disable\b"
    r"|mypy:\s*ignore"
    r"|ruff:\s*noqa\b"
    r"|fmt:\s*(?:off|on)\b"
    r"|isort:\s*skip\b"
    r"|coverage:\s*ignore\b"
    r")"
)

#: Every string literal (single- or multi-line) and every comment, matched in
#: source order. Triple-quoted alternatives come first so a docstring is one
#: match; the comment last so a ``#`` inside a string is never a comment.
STRING_OR_COMMENT = re.compile(
    r'"""(?:\\.|[^\\])*?"""'
    r"|'''(?:\\.|[^\\])*?'''"
    r'|"(?:\\.|[^"\\\n])*"'
    r"|'(?:\\.|[^'\\\n])*'"
    r"|#[^\n]*",
    re.DOTALL,
)

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BUDGET = DEFAULT_ROOT / "scripts" / "size_budget.json"


class SizeCheckError(Exception):
    """Unusable source or budget: the check fails closed."""


@dataclass(frozen=True)
class Counts:
    """Measured first-party numbers."""

    logic_lines: int
    suppression_comments: int


@dataclass(frozen=True)
class Budget:
    """Recorded numbers plus the reason for the last update."""

    logic_lines: int
    suppression_comments: int
    reason: str


def python_files(root: Path) -> list[Path]:
    """Every first-party ``*.py`` file under the covered roots, deterministically."""
    found: list[Path] = []
    for name in SOURCE_ROOTS:
        base = root / name
        if not base.is_dir():
            continue
        found.extend(
            path
            for path in sorted(base.rglob("*.py"))
            if "__pycache__" not in path.parts
        )
    return found


def count_file(path: Path) -> Counts:
    """Logic lines and suppression comments in one file.

    Strings, docstrings and comments are blanked (their line breaks kept) so
    only code lines remain; a suppression is counted only for a real comment,
    never for prose inside a string.
    """
    try:
        with tokenize.open(path) as handle:
            text = handle.read()
    except (OSError, SyntaxError, UnicodeDecodeError) as error:
        raise SizeCheckError(f"{path}: {error}") from error
    suppressions = 0

    def blank(match: re.Match[str]) -> str:
        nonlocal suppressions
        token = match.group(0)
        if token.startswith("#") and SUPPRESSION_COMMENT.match(token):
            suppressions += 1
        return "\n" * token.count("\n") or " "

    cleaned = STRING_OR_COMMENT.sub(blank, text)
    logic_lines = sum(1 for line in cleaned.splitlines() if line.strip())
    return Counts(logic_lines=logic_lines, suppression_comments=suppressions)


def count_tree(root: Path) -> Counts:
    """Measured numbers over the whole covered tree."""
    logic_lines = 0
    suppressions = 0
    for path in python_files(root):
        counts = count_file(path)
        logic_lines += counts.logic_lines
        suppressions += counts.suppression_comments
    return Counts(logic_lines=logic_lines, suppression_comments=suppressions)


def load_budget(path: Path) -> Budget:
    """Recorded numbers, validated; a missing or malformed budget fails closed."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SizeCheckError(f"cannot read {path}: {error}") from error
    if not isinstance(payload, dict):
        raise SizeCheckError(f"{path}: the budget must be a JSON object")
    values: dict[str, int] = {}
    for key in ("logic_lines", "suppression_comments"):
        value = payload.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise SizeCheckError(f"{path}: {key} must be a non-negative integer")
        values[key] = value
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip() or "\n" in reason.strip():
        raise SizeCheckError(f"{path}: reason must be one non-empty line")
    return Budget(
        logic_lines=values["logic_lines"],
        suppression_comments=values["suppression_comments"],
        reason=reason.strip(),
    )


def write_budget(path: Path, counts: Counts, reason: str) -> None:
    """Record the measured numbers with a required one-line reason."""
    text = reason.strip()
    if not text or "\n" in text:
        raise SizeCheckError("a budget update needs a one-line --reason")
    payload = {
        "logic_lines": counts.logic_lines,
        "suppression_comments": counts.suppression_comments,
        "reason": text,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def check(root: Path, budget_path: Path) -> int:
    """Compare the tree against the recorded budget; print the verdict."""
    budget = load_budget(budget_path)
    counts = count_tree(root)
    growth: list[str] = []
    if counts.logic_lines > budget.logic_lines:
        grown = counts.logic_lines - budget.logic_lines
        growth.append(
            f"logic lines {counts.logic_lines} exceed the recorded "
            f"{budget.logic_lines} (+{grown})"
        )
    if counts.suppression_comments > budget.suppression_comments:
        grown = counts.suppression_comments - budget.suppression_comments
        growth.append(
            f"suppression comments {counts.suppression_comments} exceed the recorded "
            f"{budget.suppression_comments} (+{grown})"
        )
    if growth:
        for line in growth:
            print(f"size check: {line}", file=sys.stderr)
        print(
            "size check: update "
            f"{_display(budget_path)} with the new numbers and a one-line reason "
            '(python scripts/size_budget.py --write --reason "...")',
            file=sys.stderr,
        )
        return 1
    print(
        f"size check: logic lines {counts.logic_lines}/{budget.logic_lines}, "
        f"suppression comments {counts.suppression_comments}/"
        f"{budget.suppression_comments}; within budget"
    )
    return 0


def _display(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(DEFAULT_ROOT))
    except ValueError:
        return str(path)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="First-party size budget check.")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="compare (default)")
    action.add_argument(
        "--write", action="store_true", help="record the measured numbers"
    )
    parser.add_argument("--reason", default="", help="one line for a --write update")
    parser.add_argument(
        "--root", type=Path, default=DEFAULT_ROOT, help="tree to measure"
    )
    parser.add_argument(
        "--budget", type=Path, default=None, help="recorded budget JSON"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = args.root.resolve()
    budget_path = args.budget or (root / "scripts" / "size_budget.json")
    try:
        if args.write:
            write_budget(budget_path, count_tree(root), args.reason)
            print(f"size check: recorded the measured numbers in {_display(budget_path)}")
            return 0
        return check(root, budget_path)
    except SizeCheckError as error:
        print(f"size check: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
