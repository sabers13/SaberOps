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

#: One explicit per-clause answer line.  Markdown bullets and bold markers
#: around the label are tolerated; ``<n>`` is deliberately not matched, so the
#: rendered instruction never counts as an answer.  The status token is parsed
#: from anywhere on the line after the marker (D1): a reviewer that writes
#: ``CLAUSE 1: The failing test passes. PASS — …`` still answered the clause.
_ANSWER_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*{1,2}|_{1,2})?CLAUSE\s+(\d+)"
    r"(?:\*{1,2}|_{1,2})?\s*:\s*(?P<rest>.*)$",
    re.IGNORECASE,
)

#: An explicit answer token.  Tokens are uppercase by protocol; a lowercase
#: English "pass"/"fail" in evidence prose is not an answer.  Both tokens on
#: one line are contradictory and fail closed.
_STATUS_RE = re.compile(r"\b(PASS|FAIL)\b")


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


def render_clause_format_reask(
    clauses: tuple[str, ...], previous_output: str, *, max_previous_chars: int = 4000
) -> str:
    """Render the one bounded format re-ask for an unusable clause answer.

    The reviewer already returned an overall PASS, but the deterministic gate
    could not read one answer.  This prompt asks only for the exact line form;
    it never restates the verdict or the evidence, and the engine still applies
    the unchanged fail-closed coverage check to the answer.
    """
    numbered = "\n".join(
        f"CLAUSE {number}: {clause}" for number, clause in enumerate(clauses, start=1)
    )
    previous = previous_output.strip()
    if len(previous) > max_previous_chars:
        previous = previous[:max_previous_chars] + "\n... [truncated by the engine]"
    return (
        "Clause format re-ask (one attempt; deterministic gate).\n\n"
        "Your previous answer returned an overall PASS, but the engine could "
        "not read a usable PASS/FAIL answer for every clause. That answer is "
        "kept as the review verdict; only the per-clause lines are missing.\n\n"
        "Previous answer:\n---\n"
        f"{previous}\n---\n\n"
        "Acceptance clauses (authoritative; extracted from the task):\n"
        f"{numbered}\n\n"
        "Answer every clause again, one line per clause, using the same number "
        "and this exact form:\n"
        "  CLAUSE <n>: PASS|FAIL — evidence from the candidate diff or the "
        "engine-verified evidence\n"
        "- A clause you cannot verify from the provided material is FAIL.\n"
        "- Return only the clause lines. Do not repeat the full review.\n"
    )


@dataclass(frozen=True)
class ClauseCoverage:
    """Deterministic result of checking a reviewer's clause answers."""

    missing: tuple[int, ...]
    failed: tuple[int, ...]

    @property
    def usable(self) -> bool:
        return not self.missing and not self.failed

    @property
    def format_retry_eligible(self) -> bool:
        """True only for a missing/unparseable answer, never a FAIL/conflict."""
        return bool(self.missing) and not self.failed

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
        rest = match.group("rest")
        statuses = set(_STATUS_RE.findall(rest))
        if not statuses:
            continue
        if len(statuses) > 1:
            # A line that says both PASS and FAIL is contradictory.
            answers.setdefault(number, set()).add("CONTRADICTION")
            continue
        status = statuses.pop()
        evidence = _STATUS_RE.sub(" ", rest)
        evidence = evidence.strip().lstrip("—-:*_ ").strip()
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
