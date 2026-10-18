"""Telegram bot interaction surface (TG-1, Step 018).

Optional interface milestone: the bot notifies the owner about ``Q-1``
questions, gate completions and wave milestones, answers conversational
status queries directly from stored records (never waking the
Orchestrator), and routes keyed ``/answer`` replies into the question
queue with sender authorization and idempotent delivery.
"""

from __future__ import annotations

from .answers import (
    AnswerOutcome,
    AnswerRouter,
    IncomingMessage,
    QuestionStore,
    parse_answer_command,
    parse_incoming_message,
)
from .bot import BotDaemon, HandleResult
from .client import BotApiClient, TelegramApiError, TelegramTransportError, Transport, redacted_url
from .config import (
    TELEGRAM_ALLOW_CHAT_IDS_ENV,
    TELEGRAM_ALLOW_USER_IDS_ENV,
    TELEGRAM_BOT_TOKEN_ENV,
    TELEGRAM_DIGEST_ENV,
    TELEGRAM_DND_END_ENV,
    TELEGRAM_DND_START_ENV,
    TELEGRAM_MIN_INTERVAL_ENV,
    TELEGRAM_QUIET_ENV,
    TelegramConfig,
    parse_id_list,
    parse_time_of_day,
)
from .notify import (
    GateCompletion,
    NotificationResult,
    NotificationService,
    WaveMilestone,
    format_gate_completion,
    format_question_notification,
    format_wave_milestone,
)
from .status import StatusReader, is_status_query

__all__ = [
    "TELEGRAM_ALLOW_CHAT_IDS_ENV",
    "TELEGRAM_ALLOW_USER_IDS_ENV",
    "TELEGRAM_BOT_TOKEN_ENV",
    "TELEGRAM_DIGEST_ENV",
    "TELEGRAM_DND_END_ENV",
    "TELEGRAM_DND_START_ENV",
    "TELEGRAM_MIN_INTERVAL_ENV",
    "TELEGRAM_QUIET_ENV",
    "AnswerOutcome",
    "AnswerRouter",
    "BotApiClient",
    "BotDaemon",
    "GateCompletion",
    "HandleResult",
    "IncomingMessage",
    "NotificationResult",
    "NotificationService",
    "QuestionStore",
    "StatusReader",
    "TelegramApiError",
    "TelegramConfig",
    "TelegramTransportError",
    "Transport",
    "WaveMilestone",
    "format_gate_completion",
    "format_question_notification",
    "format_wave_milestone",
    "is_status_query",
    "parse_answer_command",
    "parse_id_list",
    "parse_incoming_message",
    "parse_time_of_day",
    "redacted_url",
]
