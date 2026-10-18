"""Telegram Bot API transport (TG-1, Step 018).

A thin wrapper over the Telegram Bot HTTP API using only the standard
library, so tests and the daemon never need a third-party client.  The
transport is injectable: hermetic tests pass a fake, the daemon uses the
default ``urllib`` transport.  The bot token never appears in logs, errors
or ``repr`` output.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "BotApiClient",
    "TelegramApiError",
    "TelegramTransportError",
    "Transport",
    "redacted_url",
]

#: Transport callable: ``(api_method, params) -> result`` where ``result`` is
#: the decoded ``result`` field of the Bot API envelope.
Transport = Callable[[str, dict[str, Any]], Any]


class TelegramTransportError(ValueError):
    """The HTTP transport failed (network error or bad envelope)."""


class TelegramApiError(ValueError):
    """The Telegram Bot API answered ``ok: false`` for a method call."""

    def __init__(self, method: str, description: str) -> None:
        super().__init__(f"telegram api {method} failed: {description}")
        self.method = method
        self.description = description


def redacted_url(url: str) -> str:
    """Redact the bot token segment of a Bot API URL for safe logging."""
    marker = "/bot"
    at = url.find(marker)
    if at < 0:  # pragma: no cover - defensive; every real URL has /bot<token>/
        return url
    rest = url[at + len(marker) :]
    slash = rest.find("/")
    if slash < 0:
        return url[:at] + "/bot<redacted>"
    return url[:at] + "/bot<redacted>" + rest[slash:]


def _urllib_transport(base_url: str, timeout_s: float) -> Transport:
    """Build the default ``urllib`` transport bound to one bot token."""

    def _call(method: str, params: dict[str, Any]) -> Any:
        url = f"{base_url}/{method}"
        body = urllib.parse.urlencode(
            {key: str(value) for key, value in params.items()}
        ).encode("utf-8")
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise TelegramTransportError(
                f"telegram transport failed for {method}: {exc}"
            ) from exc
        if not isinstance(payload, dict) or "ok" not in payload:
            raise TelegramTransportError(
                f"telegram transport returned a bad envelope for {method}"
            )
        if not payload["ok"]:
            raise TelegramApiError(method, str(payload.get("description", "unknown")))
        return payload.get("result")

    return _call


@dataclass
class BotApiClient:
    """Minimal Telegram Bot API client (``sendMessage`` + ``getUpdates``)."""

    bot_token: str
    transport: Transport | None = None
    timeout_s: float = 30.0

    def __post_init__(self) -> None:
        if not isinstance(self.bot_token, str) or not self.bot_token.strip():
            raise TelegramTransportError("a bot token is required to call the Bot API")
        if self.timeout_s <= 0:
            raise TelegramTransportError("timeout_s must be positive")
        if self.transport is None:
            base = f"https://api.telegram.org/bot{self.bot_token}"
            object.__setattr__(self, "transport", _urllib_transport(base, self.timeout_s))

    def __repr__(self) -> str:  # pragma: no cover - trivial redaction guard
        return f"BotApiClient(token=<redacted>, timeout_s={self.timeout_s!r})"

    @property
    def _call(self) -> Transport:
        transport = self.transport
        if transport is None:  # pragma: no cover - post-init always sets one
            raise TelegramTransportError("no transport configured")
        return transport

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        disable_notification: bool = False,
    ) -> Any:
        """Send one text message; returns the decoded ``result`` payload."""
        if not isinstance(text, str) or not text.strip():
            raise TelegramTransportError("message text is required")
        params: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode is not None:
            params["parse_mode"] = parse_mode
        if disable_notification:
            params["disable_notification"] = "true"
        return self._call("sendMessage", params)

    def get_updates(
        self, offset: int | None = None, *, timeout: int = 30
    ) -> list[dict[str, Any]]:
        """Long-poll for updates; returns the decoded ``result`` list."""
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        result = self._call("getUpdates", params)
        if not isinstance(result, list):
            raise TelegramTransportError("getUpdates returned a non-list result")
        return [item for item in result if isinstance(item, dict)]
