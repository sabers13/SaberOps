"""Telegram bot daemon: polling loop and update dispatch (TG-1, Step 018).

:class:`BotDaemon` wires the pieces together: long-poll ``getUpdates``,
authorize the sender, answer status queries from stored records, route
``/answer`` commands into the Q-1 queue, and reply in the originating chat.
Duplicate deliveries are acknowledged without any second send or second
answer.  The daemon never imports the Orchestrator and never wakes it:
status reads are local, answers go through the queue's explicit verbs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from saberops.questions import OwnerQuestionQueue

from .answers import AnswerRouter, parse_incoming_message
from .client import BotApiClient
from .config import TelegramConfig
from .notify import NotificationService
from .status import StatusReader, is_status_query

__all__ = [
    "BotDaemon",
    "HandleResult",
]


@dataclass(frozen=True)
class HandleResult:
    """What one update produced: the reply sent (or why nothing was sent)."""

    replied: bool
    reply_text: str
    outcome: str


@dataclass
class BotDaemon:
    """One polling bot: config, transport, queue and routing in one place."""

    config: TelegramConfig
    client: BotApiClient
    queue: OwnerQuestionQueue
    notifications: NotificationService | None = None
    router: AnswerRouter | None = None
    reader: StatusReader = field(default_factory=StatusReader)
    next_offset: int | None = None

    def __post_init__(self) -> None:
        if self.router is None:
            object.__setattr__(self, "router", AnswerRouter(self.config, self.queue))

    @property
    def _router(self) -> AnswerRouter:
        assert self.router is not None
        return self.router

    def handle_update(self, payload: object, *, as_of: date | None = None) -> HandleResult:
        """Handle one raw Bot API update; never raises for bad input."""
        message = parse_incoming_message(payload)
        if message is None:
            return HandleResult(replied=False, reply_text="", outcome="ignored")
        if not self.config.is_sender_authorized(message.chat_id, message.user_id):
            return HandleResult(replied=False, reply_text="", outcome="refused")
        if is_status_query(message.text):
            reply = self.reader.answer_query(message.text, self.queue, as_of=as_of)
            if reply is None:  # pragma: no cover - is_status_query agreed above
                return HandleResult(replied=False, reply_text="", outcome="ignored")
            self.client.send_message(message.chat_id, reply)
            return HandleResult(replied=True, reply_text=reply, outcome="status")
        outcome = self._router.route(message)
        if outcome.kind == "duplicate":
            return HandleResult(replied=False, reply_text="", outcome="duplicate")
        if outcome.kind == "ignored":
            reply = (
                "I only understand status commands and answers. "
                "Send /help for the list."
            )
            self.client.send_message(message.chat_id, reply)
            return HandleResult(replied=True, reply_text=reply, outcome="ignored")
        if outcome.kind == "refused":  # pragma: no cover - checked above already
            return HandleResult(replied=False, reply_text="", outcome="refused")
        if outcome.kind == "answered":
            reply = f"✅ {outcome.detail} — stored exactly as sent."
            self.client.send_message(message.chat_id, reply)
            return HandleResult(
                replied=True, reply_text=reply, outcome="answered:" + (outcome.question_id or "")
            )
        reply = f"⚠️ {outcome.detail}"
        self.client.send_message(message.chat_id, reply)
        return HandleResult(replied=True, reply_text=reply, outcome=outcome.kind)

    def poll_once(self, *, timeout: int = 30, as_of: date | None = None) -> list[HandleResult]:
        """Fetch one batch of updates and handle each; advances the offset."""
        updates = self.client.get_updates(self.next_offset, timeout=timeout)
        results: list[HandleResult] = []
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                self.next_offset = update_id + 1
            results.append(self.handle_update(update, as_of=as_of))
        return results

    def updates_snapshot(self) -> dict[str, Any]:
        """Return daemon cursor state for diagnostics (no secrets)."""
        return {
            "next_offset": self.next_offset,
            "configured": self.config.is_configured,
            "seen_updates": len(self._router.seen_update_ids),
        }
