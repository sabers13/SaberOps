"""Deterministic acceptance-clause protocol for independent review.

A task may state an ``Acceptance:`` section.  When it does, an independent
review ``PASS`` is usable only if the reviewer answered every acceptance
clause explicitly and did not mark any clause ``FAIL``.  This module owns the
pure part of that rule: extracting the clauses, rendering the review
instructions, and checking the reviewer's per-clause answers.

It is pure: no I/O, adapters, clock or randomness.  The deterministic coverage
check is the authority; reviewer prose can never turn an unverified clause into
an accepted one (the same fail-closed shape as :mod:`saberops.review_policy`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: An acceptance heading, optionally Markdown-decorated and optionally plural.
_HEADER_RE = re.compile(
    r"^\s{0,3}(?:#{1,6}\s*)?\*{0,2}acceptance"
    r"(?:\s+(?:criteria|clauses?))?\*{0,2}\s*:\s*(?P<inline>.*)$",
    re.IGNORECASE,
)

#: A following section heading ends the acceptance block.
_STOP_HEADINGS: tuple[str, ...] = (
    "goal:",
    "notes:",
    "constraints:",
    "context:",
    "deliverables:",
    "verification:",
)

_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")

#: One explicit per-clause answer.  Markdown bullets and bold markers around
#: the label or status are tolerated; ``<n>`` is deliberately not matched, so
#: the rendered instruction never counts as an answer.
_ANSWER_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*{1,2}|_{1,2})?CLAUSE\s+(\d+)"
    r"(?:\*{1,2}|_{1,2})?\s*:\s*(?:\*{1,2}|_{1,2})?(PASS|FAIL)\b",
    re.IGNORECASE,
)


def extract_acceptance_clauses(task: str) -> tuple[str, ...]:
    """Return the clause texts in the task's ``Acceptance:`` section.

    Returns an empty tuple when the task has no acceptance section, which
    keeps the historical review contract for tasks that never declared one.
    """
    lines = task.splitlines()
    start: int | None = None
    inline = ""
    for index, line in enumerate(lines):
        match = _HEADER_RE.match(line)
        if match is not None:
            start = index
            inline = match.group("inline").strip()
            break
    if start is None:
        return ()

    collected: list[str] = [inline] if inline else []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if any(lowered.startswith(head) for head in _STOP_HEADINGS):
            break
        collected.append(_BULLET_RE.sub("", stripped))

    clauses: list[str] = []
    for item in collected:
        cleaned = item.strip()
        if not cleaned or cleaned.startswith("```") or cleaned in {"---", "..."}:
            continue
        clauses.append(cleaned)
    return tuple(clauses)


def render_acceptance_protocol(clauses: tuple[str, ...]) -> str:
    """Render the authoritative clause list and the required answer format."""
    if not clauses:
        return ""
    numbered = "\n".join(
        f"CLAUSE {number}: {clause}" for number, clause in enumerate(clauses, start=1)
    )
    return (
        "Acceptance clauses (authoritative; extracted from the task):\n"
        f"{numbered}\n\n"
        "Acceptance review protocol (deterministic; a PASS is not usable without it):\n"
        "- Verify every clause against the candidate diff AND the engine-verified "
        "evidence in this prompt. The diff shows what changed. Engine-verified "
        "evidence - full content of touched files, the modules they import, gate or "
        "test output, and engine-run repository searches - is trusted because the "
        "engine produced it from the candidate commit. Repair reports and other "
        "worker or reviewer prose are claims, not evidence.\n"
        "- Answer every clause explicitly, one line per clause, using the same number "
        "and this exact form:\n"
        "  CLAUSE <n>: PASS|FAIL — evidence from the diff or the engine-verified "
        "evidence (quote the hunk, the file content, or the gate/search output)\n"
        "- A clause you cannot verify from the provided material is FAIL.\n"
        "- The overall VERDICT: PASS is valid only when every clause answer is PASS. "
        "If the change follows the goal but violates, removes, or fails to prove any "
        "clause, the only valid verdict is VERDICT: BLOCK.\n"
        "- When your clause answers and your overall verdict disagree, the stricter "
        "outcome wins: any FAIL clause or a BLOCK/FINAL_BLOCK verdict blocks the "
        "candidate. Never return FINAL_BLOCK without stating the reason.\n"
    )


@dataclass(frozen=True)
class ClauseCoverage:
    """Deterministic result of checking a reviewer's clause answers."""

    missing: tuple[int, ...]
    failed: tuple[int, ...]

    @property
    def usable(self) -> bool:
        return not self.missing and not self.failed

    def describe(self) -> str:
        parts: list[str] = []
        if self.missing:
            parts.append(
                "missing answers for "
                + ", ".join(f"clause {number}" for number in self.missing)
            )
        if self.failed:
            parts.append(
                "failed or contradictory answers for "
                + ", ".join(f"clause {number}" for number in self.failed)
            )
        return "; ".join(parts) if parts else "every clause has a PASS answer"


def clause_coverage(output_text: str, clauses: tuple[str, ...]) -> ClauseCoverage:
    """Check that every clause has one unambiguous PASS answer.

    A missing answer, an explicit FAIL, and a contradictory pair of answers for
    the same clause all fail closed.
    """
    answers: dict[int, set[str]] = {}
    for line in output_text.splitlines():
        match = _ANSWER_RE.match(line)
        if match is None:
            continue
        number = int(match.group(1))
        status = match.group(2).upper()
        evidence = line[match.end() :].strip().lstrip("—-:*_ ").strip()
        if status == "PASS" and not evidence:
            # A verdict without evidence is not an answered clause.
            answers.setdefault(number, set()).add("NO_EVIDENCE")
            continue
        answers.setdefault(number, set()).add(status)

    missing: list[int] = []
    failed: list[int] = []
    for number in range(1, len(clauses) + 1):
        seen = answers.get(number)
        if not seen:
            missing.append(number)
        elif seen != {"PASS"}:
            failed.append(number)
    return ClauseCoverage(missing=tuple(missing), failed=tuple(failed))
