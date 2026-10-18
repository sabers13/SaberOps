"""Telegram Bot client configuration (TG-1, Step 018).

Everything in this module is local and hermetic: configuration comes only
from explicit constructor arguments or the ``SABEROPS_TELEGRAM_*``
environment variables documented in ``docs/telegram.md``.  The bot token is
never logged, never written to disk by this package, and never appears in
``repr`` output.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__all__ = [
    "TELEGRAM_ALLOW_CHAT_IDS_ENV",
    "TELEGRAM_ALLOW_USER_IDS_ENV",
    "TELEGRAM_BOT_TOKEN_ENV",
    "TELEGRAM_DIGEST_ENV",
    "TELEGRAM_DND_END_ENV",
    "TELEGRAM_DND_START_ENV",
    "TELEGRAM_MIN_INTERVAL_ENV",
    "TELEGRAM_QUIET_ENV",
    "TelegramConfig",
    "parse_id_list",
    "parse_time_of_day",
]

#: Environment variable carrying the Telegram Bot API token.
TELEGRAM_BOT_TOKEN_ENV = "SABEROPS_TELEGRAM_BOT_TOKEN"
#: Comma/space separated Telegram chat ids allowed to interact with the bot.
TELEGRAM_ALLOW_CHAT_IDS_ENV = "SABEROPS_TELEGRAM_ALLOW_CHAT_IDS"
#: Comma/space separated Telegram user ids allowed to interact with the bot.
TELEGRAM_ALLOW_USER_IDS_ENV = "SABEROPS_TELEGRAM_ALLOW_USER_IDS"
#: When truthy (``1``/``true``/``yes``), notifications are muted until cleared.
TELEGRAM_QUIET_ENV = "SABEROPS_TELEGRAM_QUIET"
#: Do-not-disturb window edges as ``HH:MM`` (24h local bot time).
TELEGRAM_DND_START_ENV = "SABEROPS_TELEGRAM_DND_START"
TELEGRAM_DND_END_ENV = "SABEROPS_TELEGRAM_DND_END"
#: Minimum seconds between two notification sends (rate limiting).
TELEGRAM_MIN_INTERVAL_ENV = "SABEROPS_TELEGRAM_MIN_INTERVAL_S"
#: When truthy, notifications are buffered and sent as one digest.
TELEGRAM_DIGEST_ENV = "SABEROPS_TELEGRAM_DIGEST"


def parse_id_list(raw: str | None, field_name: str) -> tuple[int, ...]:
    """Parse a comma/space separated id list; fail closed on bad input."""
    if raw is None or not raw.strip():
        return ()
    ids: list[int] = []
    for token in raw.replace(",", " ").split():
        try:
            value = int(token, 10)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be integer ids; got {token!r}") from exc
        ids.append(value)
    if len(set(ids)) != len(ids):
        raise ValueError(f"{field_name} must not repeat an id")
    return tuple(ids)


def parse_time_of_day(raw: str | None, field_name: str) -> tuple[int, int] | None:
    """Parse ``HH:MM`` into ``(hour, minute)``; ``None`` when unset."""
    if raw is None or not raw.strip():
        return None
    parts = raw.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"{field_name} must be HH:MM (24h); got {raw!r}")
    try:
        hour, minute = int(parts[0], 10), int(parts[1], 10)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be HH:MM (24h); got {raw!r}") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{field_name} must be HH:MM (24h); got {raw!r}")
    return (hour, minute)


def _parse_flag(raw: str | None) -> bool:
    return (raw or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class TelegramConfig:
    """Local Telegram posture: who may talk, when the bot stays quiet.

    An empty ``allowed_chat_ids`` *and* empty ``allowed_user_ids`` means no
    sender is authorized: every incoming update is refused.  At least one
    whitelist entry is required before the bot answers anything.
    """

    bot_token: str = ""
    allowed_chat_ids: tuple[int, ...] = ()
    allowed_user_ids: tuple[int, ...] = ()
    quiet: bool = False
    dnd_start: tuple[int, int] | None = None
    dnd_end: tuple[int, int] | None = None
    min_interval_s: float = 0.0
    digest: bool = False
    default_chat_id: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.bot_token, str):
            raise ValueError("bot_token must be text")
        if not isinstance(self.allowed_chat_ids, tuple):
            object.__setattr__(self, "allowed_chat_ids", tuple(self.allowed_chat_ids))
        if not isinstance(self.allowed_user_ids, tuple):
            object.__setattr__(self, "allowed_user_ids", tuple(self.allowed_user_ids))
        if self.min_interval_s < 0:
            raise ValueError("min_interval_s must be >= 0")
        if self.default_chat_id is not None and not isinstance(self.default_chat_id, int):
            raise ValueError("default_chat_id must be an integer chat id")

    def __repr__(self) -> str:  # pragma: no cover - trivial redaction guard
        return (
            "TelegramConfig(token=<redacted>, "
            f"allowed_chat_ids={self.allowed_chat_ids!r}, "
            f"allowed_user_ids={self.allowed_user_ids!r}, "
            f"quiet={self.quiet!r}, dnd_start={self.dnd_start!r}, "
            f"dnd_end={self.dnd_end!r}, min_interval_s={self.min_interval_s!r}, "
            f"digest={self.digest!r}, default_chat_id={self.default_chat_id!r})"
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> TelegramConfig:
        """Build configuration from the process environment (or ``env``)."""
        source = env if env is not None else os.environ
        raw_interval = (source.get(TELEGRAM_MIN_INTERVAL_ENV) or "").strip()
        interval = 0.0
        if raw_interval:
            try:
                interval = float(raw_interval)
            except ValueError as exc:
                raise ValueError(
                    f"{TELEGRAM_MIN_INTERVAL_ENV} must be a number of seconds; "
                    f"got {raw_interval!r}"
                ) from exc
        return cls(
            bot_token=(source.get(TELEGRAM_BOT_TOKEN_ENV) or "").strip(),
            allowed_chat_ids=parse_id_list(
                source.get(TELEGRAM_ALLOW_CHAT_IDS_ENV), TELEGRAM_ALLOW_CHAT_IDS_ENV
            ),
            allowed_user_ids=parse_id_list(
                source.get(TELEGRAM_ALLOW_USER_IDS_ENV), TELEGRAM_ALLOW_USER_IDS_ENV
            ),
            quiet=_parse_flag(source.get(TELEGRAM_QUIET_ENV)),
            dnd_start=parse_time_of_day(
                source.get(TELEGRAM_DND_START_ENV), TELEGRAM_DND_START_ENV
            ),
            dnd_end=parse_time_of_day(
                source.get(TELEGRAM_DND_END_ENV), TELEGRAM_DND_END_ENV
            ),
            min_interval_s=interval,
            digest=_parse_flag(source.get(TELEGRAM_DIGEST_ENV)),
        )

    @property
    def is_configured(self) -> bool:
        """True when a token and at least one whitelist entry exist."""
        return bool(self.bot_token) and bool(
            self.allowed_chat_ids or self.allowed_user_ids
        )

    def is_sender_authorized(self, chat_id: int, user_id: int | None) -> bool:
        """Return True when the chat or the user is explicitly whitelisted."""
        if chat_id in self.allowed_chat_ids:
            return True
        return user_id is not None and user_id in self.allowed_user_ids

    def in_dnd(self, hour: int, minute: int) -> bool:
        """Return True when ``(hour, minute)`` falls inside the DND window."""
        if self.dnd_start is None or self.dnd_end is None:
            return False
        start = self.dnd_start[0] * 60 + self.dnd_start[1]
        end = self.dnd_end[0] * 60 + self.dnd_end[1]
        now = hour * 60 + minute
        if start <= end:
            return start <= now < end
        return now >= start or now < end
