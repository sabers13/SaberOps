"""Telegram notification formatting and quiet-posture dispatch (TG-1, Step 018).

Formatting renders owner questions (``Q-1``), gate completions and wave
milestones as plain-text Telegram messages.  Dispatch applies the quiet/away
posture: an explicit quiet flag, a do-not-disturb window, per-kind rate
limiting, and per-question de-duplication so a re-notify never spams the
owner.  Nothing here wakes the Orchestrator; notification reads already
stored records passed in by the caller.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from saberops.questions import OwnerQuestion

from .config import TelegramConfig

__all__ = [
    "GateCompletion",
    "NotificationResult",
    "NotificationService",
    "WaveMilestone",
    "format_gate_completion",
    "format_question_notification",
    "format_wave_milestone",
]

MAX_MESSAGE_CHARS = 3500


def _clip(text: str, limit: int = 1200) -> str:
    cleaned = " ".join(str(text).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


@dataclass(frozen=True)
class GateCompletion:
    """One gate outcome to announce (plain data; no engine import)."""

    project: str
    lane: str
    gate: str
    outcome: str
    head_sha: str = ""
    detail: str = ""


@dataclass(frozen=True)
class WaveMilestone:
    """One wave status event to announce (plain data; no engine import)."""

    wave: str
    status: str
    detail: str = ""


def format_question_notification(question: OwnerQuestion) -> str:
    """Render one owner question as an actionable Telegram message."""
    lines = [
        f"❓ Owner question: {question.key}",
        f"Subject: {_clip(question.request.subject, 500)}",
        (
            f"Kind: {question.kind} · "
            f"Decision: {question.request.decision_class.value} · "
            f"Status: {question.status.value}"
        ),
    ]
    if question.request.blocks:
        lines.append("Blocks: " + ", ".join(question.request.blocks))
    if question.request.options:
        lines.append("Options: " + " | ".join(question.request.options))
        if question.request.default_option is not None:
            lines.append(f"Default: {question.request.default_option}")
    lines.append("Why this needs you:")
    for part in (
        "requirement",
        "proposed_expansion",
        "smallest_compliant",
        "consequences",
        "recommendation",
    ):
        lines.append(f"• {part}: {_clip(getattr(question.request.escalation, part), 400)}")
    lines.append(f"Reply with: /answer {question.key} <your exact words>")
    text = "\n".join(lines)
    return text if len(text) <= MAX_MESSAGE_CHARS else text[: MAX_MESSAGE_CHARS - 1] + "…"


def format_gate_completion(event: GateCompletion) -> str:
    """Render one gate completion as a one-shot status message."""
    head = f" · {event.head_sha[:12]}" if event.head_sha else ""
    lines = [f"✅ Gate {event.gate}: {event.outcome}{head}"]
    lines.append(f"Project: {event.project} · Lane: {event.lane}")
    if event.detail:
        lines.append(_clip(event.detail, 800))
    return "\n".join(lines)


def format_wave_milestone(event: WaveMilestone) -> str:
    """Render one wave milestone as a one-shot status message."""
    lines = [f"🌊 Wave {event.wave}: {event.status}"]
    if event.detail:
        lines.append(_clip(event.detail, 800))
    return "\n".join(lines)


@dataclass(frozen=True)
class NotificationResult:
    """The outcome of one dispatch attempt (sent or suppressed with a reason)."""

    sent: bool
    reason: str
    text: str = ""
    chat_id: int | None = None


@dataclass
class NotificationService:
    """Quiet-aware notification dispatcher over a send callable.

    ``send`` is ``(chat_id, text) -> Any``; the daemon passes
    :meth:`BotApiClient.send_message`, tests pass a recording fake.  All
    clock reads come from the caller-supplied ``now`` so tests never touch
    the real clock.
    """

    config: TelegramConfig
    send: Callable[[int, str], Any]
    notified_question_ids: set[str] = field(default_factory=set)
    last_sent_at: dict[str, float] = field(default_factory=dict)
    digest_buffer: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not callable(self.send):
            raise ValueError("a send callable (chat_id, text) is required")

    def _default_chat(self) -> int | None:
        if self.config.default_chat_id is not None:
            return self.config.default_chat_id
        if self.config.allowed_chat_ids:
            return self.config.allowed_chat_ids[0]
        return None

    def _posture_block(self, now: datetime, now_ts: float, kind: str) -> str | None:
        if self.config.quiet:
            return "quiet"
        if self.config.in_dnd(now.hour, now.minute):
            return "dnd"
        interval = self.config.min_interval_s
        if interval > 0:
            previous = self.last_sent_at.get(kind)
            if previous is not None and now_ts - previous < interval:
                return "rate-limited"
        return None

    def _deliver(
        self, chat_id: int | None, text: str, kind: str, now_ts: float
    ) -> NotificationResult:
        if chat_id is None:
            return NotificationResult(sent=False, reason="no-chat", text=text)
        if self.config.digest:
            self.digest_buffer.append(text)
            return NotificationResult(sent=False, reason="digested", text=text, chat_id=chat_id)
        send = self.send
        assert callable(send)
        send(chat_id, text)
        self.last_sent_at[kind] = now_ts
        return NotificationResult(sent=True, reason="sent", text=text, chat_id=chat_id)

    def notify_question(
        self,
        question: OwnerQuestion,
        *,
        now: datetime,
        now_ts: float | None = None,
        chat_id: int | None = None,
        force: bool = False,
    ) -> NotificationResult:
        """Notify the owner about one question (deduped per question id)."""
        stamp = now_ts if now_ts is not None else now.timestamp()
        target = chat_id if chat_id is not None else self._default_chat()
        if not force and question.question_id in self.notified_question_ids:
            return NotificationResult(
                sent=False, reason="duplicate", text="", chat_id=target
            )
        blocked = None if force else self._posture_block(now, stamp, "question")
        if blocked is not None:
            return NotificationResult(sent=False, reason=blocked, text="", chat_id=target)
        text = format_question_notification(question)
        result = self._deliver(target, text, "question", stamp)
        if result.sent or result.reason == "digested":
            self.notified_question_ids.add(question.question_id)
        return result

    def notify_gate_completion(
        self,
        event: GateCompletion,
        *,
        now: datetime,
        now_ts: float | None = None,
        chat_id: int | None = None,
    ) -> NotificationResult:
        """Notify the owner about one gate completion."""
        stamp = now_ts if now_ts is not None else now.timestamp()
        target = chat_id if chat_id is not None else self._default_chat()
        blocked = self._posture_block(now, stamp, "gate")
        if blocked is not None:
            return NotificationResult(sent=False, reason=blocked, text="", chat_id=target)
        return self._deliver(target, format_gate_completion(event), "gate", stamp)

    def notify_wave_milestone(
        self,
        event: WaveMilestone,
        *,
        now: datetime,
        now_ts: float | None = None,
        chat_id: int | None = None,
    ) -> NotificationResult:
        """Notify the owner about one wave milestone."""
        stamp = now_ts if now_ts is not None else now.timestamp()
        target = chat_id if chat_id is not None else self._default_chat()
        blocked = self._posture_block(now, stamp, "wave")
        if blocked is not None:
            return NotificationResult(sent=False, reason=blocked, text="", chat_id=target)
        return self._deliver(target, format_wave_milestone(event), "wave", stamp)

    def flush_digest(
        self, *, chat_id: int | None = None, now_ts: float | None = None
    ) -> NotificationResult:
        """Send all buffered digest messages as one combined message."""
        if not self.digest_buffer:
            return NotificationResult(sent=False, reason="empty", text="")
        target = chat_id if chat_id is not None else self._default_chat()
        if target is None:
            return NotificationResult(sent=False, reason="no-chat", text="")
        combined = "\n\n---\n\n".join(self.digest_buffer)
        if len(combined) > MAX_MESSAGE_CHARS:
            combined = combined[: MAX_MESSAGE_CHARS - 1] + "…"
        send = self.send
        assert callable(send)
        send(target, combined)
        self.digest_buffer.clear()
        if now_ts is not None:
            for kind in ("question", "gate", "wave"):
                self.last_sent_at[kind] = now_ts
        return NotificationResult(sent=True, reason="sent", text=combined, chat_id=target)
