"""F19 scout jobs: a read-only investigation that delivers a report.

A scout is a job *kind* on the build route, never a fourth role.  The worker
runs with the CLI's own read-only permission rules on the run's scratch copy;
the engine writes the report from the worker's final answer into the plain
per-run folder E4 already keeps (``<state dir>/jobs/<run_id>/``).

Nothing else the worker may have changed is kept: any diff against the base is
discarded (the scratch copy is reset) and recorded as a violation on the job.
A scout never produces a candidate, so there is no gate, no independent review
and no owner acceptance: the job completes when a non-empty report exists.

The report is indexed on the job record as a ``scout_report`` job event, so a
fresh planning session can retrieve the same path, summary and findings from
``catch_up`` without the original session.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from saberops.job_evidence import _atomic_write_text, job_folder

SCOUT_REPORT_FILENAME: str = "scout-report.md"
SCOUT_REPORT_EVENT: str = "scout_report"
SCOUT_VIOLATION_EVENT: str = "scout_write_violation"
#: Bound on the worker answer that becomes a report (a report, not a dump).
MAX_REPORT_BODY_CHARS: int = 200_000
#: Bound on the short summary/finding excerpts the tools return.
MAX_EXCERPT_CHARS: int = 400
_MAX_DECISIONS: int = 20

SCOUT_PROMPT_SECTION: str = """[SCOUT JOB — READ ONLY]

This is a scout job: investigate and report. Do not modify, create, delete or
commit any file. The engine runs you with read-only permission rules, and any
change you make is discarded and recorded as a violation.

Answer with a Markdown report as your final message. Use these sections:

## Summary
Two or three sentences answering the question.

## Findings
One bullet per finding, each with the evidence that supports it.

## Owner decisions
Only decisions the owner must make. One bullet per decision, phrased as the
question to answer. Leave the section out when there are none.
"""


@dataclass(frozen=True)
class ScoutReport:
    """One scout deliverable, as indexed on the job record."""

    run_id: str
    report_path: str
    summary: str | None
    findings: tuple[str, ...]
    decisions: tuple[str, ...]
    violations: tuple[str, ...]
    digest: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "report_path": self.report_path,
            "summary": self.summary,
            "findings": list(self.findings),
            "decisions": list(self.decisions),
            "violations": list(self.violations),
            "digest": self.digest,
        }


def _excerpt(text: str, limit: int = MAX_EXCERPT_CHARS) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _sections(text: str) -> dict[str, str]:
    """Split a Markdown report into ``{heading_lower: body}``.

    Only level-2 style headings (``## ...``) delimit sections; nested headings
    stay inside their parent so a bullet never leaks between sections.
    """
    sections: dict[str, str] = {}
    current = ""
    buffer: list[str] = []
    for line in text.splitlines():
        match = re.match(r"^##+\s+(.+?)\s*$", line)
        if match:
            if current:
                sections[current] = "\n".join(buffer).strip()
            current = match.group(1).strip().lower()
            buffer = []
            continue
        buffer.append(line)
    if current:
        sections[current] = "\n".join(buffer).strip()
    return sections


def _bullet_lines(body: str) -> tuple[str, ...]:
    """Return the bullet/numbered items of one section, text only."""
    items: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        match = re.match(r"^(?:[-*+]|\d+[.)])\s+(.*)$", line)
        if match is None:
            continue
        text = match.group(1).strip()
        if text and text not in items:
            items.append(text)
    return tuple(items)


def _section(sections: dict[str, str], *needles: str) -> str | None:
    for name, body in sections.items():
        if any(needle in name for needle in needles):
            return body
    return None


def parse_report(text: str) -> dict[str, Any]:
    """Extract the summary, findings and owner decisions from a report body."""
    sections = _sections(text)
    summary_body = _section(sections, "summary")
    summary: str | None
    if summary_body:
        summary = _excerpt(summary_body)
    else:
        preamble = text.split("##", 1)[0].strip()
        summary = _excerpt(preamble) if preamble else None
    findings_body = _section(sections, "finding", "result")
    findings = _bullet_lines(findings_body) if findings_body else ()
    if not findings:
        findings = tuple(
            item
            for name, body in sections.items()
            if "decision" not in name
            for item in _bullet_lines(body)
        )
    decisions_body = _section(sections, "decision", "question")
    decisions = _bullet_lines(decisions_body) if decisions_body else ()
    return {
        "summary": summary or None,
        "findings": tuple(findings),
        "decisions": tuple(decisions[:_MAX_DECISIONS]),
    }


def render_report(
    *,
    run_id: str,
    job_id: str | None,
    goal: str,
    provider: str | None,
    model: str | None,
    body: str,
) -> str:
    """Compose the saved Markdown report around the worker's answer."""
    header = [
        "# Scout report",
        "",
        f"- run: {run_id}",
        f"- job: {job_id or '(not indexed yet)'}",
        f"- worker: {provider or 'unknown'}/{model or 'unknown'}",
        "- mode: read-only (no code change, no gate, no review, no acceptance)",
        "",
        "## Question",
        "",
        goal.strip(),
        "",
        "## Report",
        "",
        body.strip(),
    ]
    return "\n".join(header).rstrip() + "\n"


def write_report(
    folder: Path,
    *,
    run_id: str,
    job_id: str | None,
    goal: str,
    provider: str | None,
    model: str | None,
    body: str,
) -> tuple[Path, ScoutReport]:
    """Write the report into the run's E4 folder and return it with its index."""
    cleaned = body.strip()
    if not cleaned:
        raise ValueError("a scout report must not be empty")
    rendered = render_report(
        run_id=run_id,
        job_id=job_id,
        goal=goal,
        provider=provider,
        model=model,
        body=cleaned[:MAX_REPORT_BODY_CHARS],
    )
    path = folder / SCOUT_REPORT_FILENAME
    _atomic_write_text(path, rendered)
    parsed = parse_report(cleaned[:MAX_REPORT_BODY_CHARS])
    report = ScoutReport(
        run_id=run_id,
        report_path=str(path),
        summary=parsed["summary"],
        findings=tuple(parsed["findings"]),
        decisions=tuple(parsed["decisions"]),
        violations=(),
        digest=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
    )
    return path, report


def report_folder(state_dir: Path | str, run_id: str) -> Path:
    """The E4 per-run folder that holds this scout's report."""
    return job_folder(state_dir, run_id)


def record_report(
    db: Any,
    *,
    job_id: str,
    report: ScoutReport,
) -> None:
    """Index the report on the job record (one durable job event)."""
    from saberops.jobs import store

    store.append_event(
        db,
        job_id,
        SCOUT_REPORT_EVENT,
        report.to_payload(),
    )


def latest_report(db: Any, job_id: str) -> dict[str, Any] | None:
    """Return the latest indexed scout report for ``job_id``, if any."""
    from saberops.jobs import store

    for event in reversed(store.list_events(db, job_id)):
        if event.get("kind") != SCOUT_REPORT_EVENT:
            continue
        payload = event.get("payload_json")
        if isinstance(payload, str):
            import json

            try:
                decoded = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(decoded, dict):
                return decoded
        elif isinstance(payload, dict):
            return payload
    return None


def raise_decision_questions(
    *,
    job_id: str,
    run_id: str,
    report_path: str,
    decisions: tuple[str, ...],
    queue: Any | None = None,
) -> tuple[str, ...]:
    """Turn a report's owner-decision bullets into durable owner questions.

    The report is prose; a decision that stays only in prose is lost.  Each
    bullet becomes one keyed, owner-only question (``OTHER`` fails closed to
    the owner), idempotent across re-runs of the same report.  Returns the
    question ids that exist afterwards.
    """
    from saberops.questions import (
        AskRole,
        DecisionClass,
        Escalation,
        OwnerQuestionQueue,
        QuestionRequest,
    )

    if not decisions:
        return ()
    target = queue if queue is not None else OwnerQuestionQueue()
    ids: list[str] = []
    for index, decision in enumerate(decisions):
        digest = hashlib.sha256(decision.encode("utf-8")).hexdigest()[:12]
        key = f"scout:{job_id}:{index}:{digest}"
        request = QuestionRequest(
            key=key,
            subject=_excerpt(decision, 180),
            kind="scout_decision",
            decision_class=DecisionClass.OTHER,
            raised_by_role=AskRole.ENGINE,
            raised_by="scout",
            escalation=Escalation(
                requirement=(
                    "The scout report lists this open owner decision: "
                    f"{_excerpt(decision, 300)}"
                ),
                proposed_expansion=(
                    "Answer the decision so the follow-up build job can rely on it "
                    "instead of guessing."
                ),
                smallest_compliant=(
                    "Answer with the smallest decision that resolves it; the scout "
                    "changed no code, so no code change is required."
                ),
                consequences=(
                    "Without an answer the decision stays unresolved and any build "
                    f"job promoted from this report must not assume it. Report: {report_path}"
                ),
                recommendation=(
                    "Read the report section and answer in the owner's own words."
                ),
            ),
            scope=report_path,
            run_id=run_id,
        )
        ids.append(target.ask(request).question_id)
    return tuple(ids)


__all__ = [
    "MAX_EXCERPT_CHARS",
    "MAX_REPORT_BODY_CHARS",
    "SCOUT_PROMPT_SECTION",
    "SCOUT_REPORT_EVENT",
    "SCOUT_REPORT_FILENAME",
    "SCOUT_VIOLATION_EVENT",
    "ScoutReport",
    "latest_report",
    "parse_report",
    "raise_decision_questions",
    "record_report",
    "render_report",
    "report_folder",
    "write_report",
]
