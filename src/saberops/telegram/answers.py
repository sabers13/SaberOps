"""Telegram answer routing into the Q-1 owner question queue (TG-1, Step 018).

Keyed owner answers sent over Telegram feed directly into
:class:`saberops.questions.OwnerQuestionQueue` (aliased here as
``QuestionStore`` per the TG-1 contract).  Routing is fail-closed:

* unknown or unauthorized chats/senders are refused before the queue is
  touched — nothing is read, written or answered;
* every processed Telegram ``update_id`` (and every ``(chat, message)``
  pair) is remembered, so a duplicate webhook delivery or a retried poll
  never re-answers and never duplicates a record;
* only messages matching the ``/answer`` grammar reach the queue; the
  owner's words are stored exactly as sent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from saberops.questions import (
    AnswerAuthority,
    AskRole,
    OwnerQuestionQueue,
    QuestionError,
)

from .config import TelegramConfig

__all__ = [
    "AnswerOutcome",
    "AnswerRouter",
    "IncomingMessage",
    "QuestionStore",
    "parse_answer_command",
    "parse_incoming_message",
]

#: The queue class behind the TG-1 ``QuestionStore`` contract name.
QuestionStore = OwnerQuestionQueue


@dataclass(frozen=True)
class IncomingMessage:
    """One Telegram text message with its idempotency coordinates."""

    update_id: int
    message_id: int
    chat_id: int
    user_id: int | None
    text: str


@dataclass(frozen=True)
class AnswerOutcome:
    """The routing verdict for one incoming message."""

    kind: str  # answered | duplicate | refused | unknown | invalid | ignored
    detail: str
    question_id: str | None = None


def parse_incoming_message(payload: object) -> IncomingMessage | None:
    """Extract a text message from a Bot API update; ``None`` when unusable."""
    if not isinstance(payload, dict):
        return None
    update_id = payload.get("update_id")
    message = payload.get("message") or payload.get("edited_message")
    if not isinstance(update_id, int) or not isinstance(message, dict):
        return None
    text = message.get("text")
    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(text, str) or not text.strip():
        return None
    if not isinstance(chat, dict) or not isinstance(chat.get("id"), int):
        return None
    message_id = message.get("message_id")
    if not isinstance(message_id, int):
        return None
    user_id = sender.get("id") if isinstance(sender, dict) else None
    return IncomingMessage(
        update_id=update_id,
        message_id=message_id,
        chat_id=chat["id"],
        user_id=user_id if isinstance(user_id, int) else None,
        text=text,
    )


def parse_answer_command(text: str) -> tuple[str, str] | None:
    """Parse ``/answer <ref> <words>``; ``None`` when the text is not an answer.

    The ``/answer`` prefix accepts an optional ``@BotName`` suffix.  The
    reference is the question key or id; the words are the owner's exact
    answer text, preserved verbatim after a single separating whitespace run.
    """
    cleaned = text.strip()
    if not cleaned.startswith("/answer"):
        return None
    rest = cleaned[len("/answer") :]
    if rest.startswith("@"):
        rest = rest.split(None, 1)[1] if len(rest.split(None, 1)) > 1 else ""
    rest = rest.strip()
    if not rest:
        return None
    ref, _, words = rest.partition(" ")
    words = words.strip()
    if not ref.strip() or not words:
        return None
    return (ref.strip(), words)


@dataclass
class AnswerRouter:
    """Routes authorized ``/answer`` messages into the question queue."""

    config: TelegramConfig
    queue: OwnerQuestionQueue
    seen_update_ids: set[int] = field(default_factory=set)
    seen_messages: set[tuple[int, int]] = field(default_factory=set)

    def route(self, message: IncomingMessage) -> AnswerOutcome:
        """Authorize, deduplicate and answer; never raises for bad input."""
        if not self.config.is_sender_authorized(message.chat_id, message.user_id):
            return AnswerOutcome(kind="refused", detail="sender is not whitelisted")
        coords = (message.chat_id, message.message_id)
        if message.update_id in self.seen_update_ids or coords in self.seen_messages:
            return AnswerOutcome(kind="duplicate", detail="update already processed")
        parsed = parse_answer_command(message.text)
        if parsed is None:
            return AnswerOutcome(kind="ignored", detail="not an /answer command")
        ref, words = parsed
        try:
            question = self.queue.answer(
                ref,
                words=words,
                authority=AnswerAuthority.OWNER,
                answered_by=f"telegram:user:{message.user_id}",
                answered_by_role=AskRole.OWNER,
            )
        except QuestionError as exc:
            text = str(exc)
            kind = "unknown" if "unknown question" in text else "invalid"
            return AnswerOutcome(kind=kind, detail=text)
        self.seen_update_ids.add(message.update_id)
        self.seen_messages.add(coords)
        if question.divergent_records:
            return AnswerOutcome(
                kind="answered",
                detail=(
                    f"recorded divergent answer for {question.key}; "
                    "the earlier answer stands"
                ),
                question_id=question.question_id,
            )
        return AnswerOutcome(
            kind="answered",
            detail=f"recorded answer for {question.key}",
            question_id=question.question_id,
        )

    def route_payload(self, payload: object) -> AnswerOutcome:
        """Parse one raw Bot API update and route it (``ignored`` when empty)."""
        message = parse_incoming_message(payload)
        if message is None:
            return AnswerOutcome(kind="ignored", detail="no routable text message")
        return self.route(message)
