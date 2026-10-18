"""Conversational status queries over stored records (TG-1, Step 018).

Status questions (``/status``, ``/pending``, ``/question <ref>``) are
answered directly from the durable question document and the snapshots the
caller passes in.  This module never wakes the Orchestrator: it performs no
writes, holds no locks, imports no engine modules, and calls only the
read-only verbs of :class:`saberops.questions.OwnerQuestionQueue`.
"""

from __future__ import annotations

from datetime import date

from saberops.questions import OwnerQuestion, OwnerQuestionQueue, QuestionStatus, question_view

__all__ = [
    "StatusReader",
    "is_status_query",
]

HELP_TEXT = (
    "SaberOps status commands (answered from stored records):\n"
    "/status — open/deferred-due counts plus divergences\n"
    "/pending — every open question awaiting an owner answer\n"
    "/question <key-or-id> — one question with its five escalation parts\n"
    "/answer <key-or-id> <your exact words> — answer a question\n"
    "/help — this list"
)


def is_status_query(text: str) -> bool:
    """Return True when the text is a read-only status command."""
    cleaned = text.strip().split(None, 1)[0] if text.strip() else ""
    base = cleaned.split("@")[0].lower()
    return base in {"/status", "/pending", "/questions", "/question", "/help", "/start"}


def _one_line(question: OwnerQuestion, as_of: date | None) -> str:
    view = question_view(question, as_of)
    subject = str(view["subject"])
    if len(subject) > 90:
        subject = subject[:89].rstrip() + "…"
    return f"• {view['key']} [{view['status']}] — {subject}"


class StatusReader:
    """Read-only status answers backed by stored records."""

    def answer_query(
        self,
        text: str,
        queue: OwnerQuestionQueue,
        *,
        as_of: date | None = None,
    ) -> str | None:
        """Answer one status command; ``None`` when the text is not one."""
        if not is_status_query(text):
            return None
        cleaned = text.strip()
        command = cleaned.split(None, 1)[0].split("@")[0].lower()
        argument = cleaned.split(None, 1)[1].strip() if len(cleaned.split(None, 1)) > 1 else ""
        today = as_of or date.today()
        if command in {"/help", "/start"}:
            return HELP_TEXT
        if command in {"/status", "/pending", "/questions"}:
            pending = queue.pending(today)
            divergences = queue.divergences()
            if command == "/status":
                lines = [
                    f"📋 {len(pending)} question(s) need the owner; "
                    f"{len(divergences)} divergent record(s) on file."
                ]
            else:
                lines = [f"📋 {len(pending)} question(s) awaiting an owner answer:"]
            for question in pending:
                lines.append(_one_line(question, today))
            if command == "/status" and divergences:
                lines.append("Divergences (earlier answers stand):")
                for report in divergences:
                    lines.append(f"• {report.key}: {report.record.reason.value}")
            if not pending:
                lines.append("Nothing is waiting — the queue is clear.")
            return "\n".join(lines)
        if command == "/question":
            if not argument:
                return "Usage: /question <key-or-id>"
            found = queue.get(argument)
            if found is None:
                return f"Unknown question: {argument!r}"
            view_question: OwnerQuestion = found
            lines = [
                f"❓ {view_question.key} [{view_question.status.value}]",
                f"Subject: {view_question.request.subject}",
                f"Kind: {view_question.kind} · "
                f"Decision: {view_question.request.decision_class.value}",
            ]
            if view_question.status is QuestionStatus.ANSWERED and view_question.answer is not None:
                lines.append(f"Answer: {view_question.answer.words}")
            elif view_question.status is QuestionStatus.DEFERRED:
                lines.append(f"Deferred until: {view_question.defer_until}")
            elif view_question.status is QuestionStatus.MOOT:
                lines.append(f"Moot evidence: {view_question.moot_evidence}")
            lines.append("Escalation:")
            for part in (
                "requirement",
                "proposed_expansion",
                "smallest_compliant",
                "consequences",
                "recommendation",
            ):
                lines.append(f"• {part}: {getattr(view_question.request.escalation, part)}")
            return "\n".join(lines)
        return HELP_TEXT  # pragma: no cover - every status command is handled above
