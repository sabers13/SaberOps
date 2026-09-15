"""Structured backlog models and helpers for PASS_WITH_BACKLOG reviews."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class BacklogEntry:
    """Structured non-blocking backlog entry recorded from a review."""

    title: str
    originating_run_id: str
    review_number: int
    residual_flaw: str
    why_non_blocking_now: str
    recommended_remediation: str


def format_backlog_entry(entry: BacklogEntry) -> str:
    """Render a BacklogEntry into canonical Markdown format."""
    return (
        f"## {entry.title}\n"
        f"- **Originating Run ID**: {entry.originating_run_id}\n"
        f"- **Review Number**: {entry.review_number}\n"
        f"- **Residual Flaw**: {entry.residual_flaw}\n"
        f"- **Why Non-Blocking Now**: {entry.why_non_blocking_now}\n"
        f"- **Recommended Remediation**: {entry.recommended_remediation}\n"
    )


def parse_backlog_entries(
    text: str,
    originating_run_id: str,
    review_number: int,
    default_summary: str = "",
) -> list[BacklogEntry]:
    """Parse review feedback text into structured BacklogEntry objects.

    Supports explicitly structured markdown sections, structured bullet lists,
    or falls back to a clean entry synthesized from the review summary/details.
    """
    entries: list[BacklogEntry] = []

    # Case 1: Match structured markdown ## headers followed by bullet fields
    section_pattern = re.compile(
        r"##\s+(.+?)\n"
        r"(?:-\s+\*\*Originating Run ID\*\*:\s*(.*?)\n)?"
        r"(?:-\s+\*\*Review Number\*\*:\s*(.*?)\n)?"
        r"-\s+\*\*Residual Flaw\*\*:\s*(.*?)\n"
        r"-\s+\*\*Why Non-Blocking Now\*\*:\s*(.*?)\n"
        r"-\s+\*\*Recommended Remediation\*\*:\s*(.*?)(?=\n##|\Z)",
        re.DOTALL,
    )
    for m in section_pattern.finditer(text):
        title = m.group(1).strip()
        run_id_val = m.group(2).strip() if m.group(2) else originating_run_id
        rev_num_raw = m.group(3).strip() if m.group(3) else ""
        rev_num_val = int(rev_num_raw) if rev_num_raw.isdigit() else review_number
        residual = m.group(4).strip()
        why = m.group(5).strip()
        remediation = m.group(6).strip()
        entries.append(
            BacklogEntry(
                title=title,
                originating_run_id=run_id_val,
                review_number=rev_num_val,
                residual_flaw=residual,
                why_non_blocking_now=why,
                recommended_remediation=remediation,
            )
        )

    if entries:
        return entries

    # Case 2: Match individual bullet lines (e.g. "- [Title]: [Flaw]")
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().upper().startswith("VERDICT:")
    ]
    bullet_items = [
        line[1:].strip()
        for line in lines
        if line.startswith(("-", "*", "•")) and len(line[1:].strip()) > 3
    ]

    if bullet_items:
        for idx, item in enumerate(bullet_items):
            if ":" in item:
                title_part, detail_part = item.split(":", 1)
                title = title_part.strip()
                detail = detail_part.strip()
            else:
                title = f"Review observation {idx + 1}"
                detail = item.strip()
            entries.append(
                BacklogEntry(
                    title=title,
                    originating_run_id=originating_run_id,
                    review_number=review_number,
                    residual_flaw=detail or "Non-blocking observation recorded during review.",
                    why_non_blocking_now="Non-blocking code quality or debt item deferrable.",
                    recommended_remediation="Address in follow-up task per recommendation.",
                )
            )
        return entries

    # Case 3: Fallback synthesized from default summary and text
    title = default_summary.strip() or (lines[0] if lines else "Review Backlog Item")
    if title.upper().startswith("VERDICT:"):
        title = "Review Backlog Item"
    details_text = "\n".join(lines).strip() or "Non-blocking observation recorded during review."
    entries.append(
        BacklogEntry(
            title=title[:80],
            originating_run_id=originating_run_id,
            review_number=review_number,
            residual_flaw=details_text,
            why_non_blocking_now="Approved with non-blocking backlog on final review convergence.",
            recommended_remediation="Plan and implement follow-up remediation in subsequent task.",
        )
    )
    return entries


def append_backlog_entries(backlog_path: Path, entries: list[BacklogEntry]) -> None:
    """Append structured entries to the canonical BACKLOG.md file."""
    if not entries:
        return

    content = ""
    if backlog_path.exists():
        content = backlog_path.read_text(encoding="utf-8")
    else:
        content = "# Backlog\n\n"

    if content and not content.endswith("\n"):
        content += "\n"
    if content and not content.endswith("\n\n"):
        content += "\n"

    for entry in entries:
        content += format_backlog_entry(entry) + "\n"

    backlog_path.write_text(content, encoding="utf-8")
