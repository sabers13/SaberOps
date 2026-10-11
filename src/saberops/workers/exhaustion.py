"""Plan-exhaustion classification for native CLI worker adapters (T-004).

Implements the R-001 classification table (``.orch/R-001/findings.md``):
one pure classifier per CLI mapping captured CLI output to
``QUOTA_EXHAUSTED`` (do not retry until reset) | ``RATE_LIMITED``
(short backoff, keep today's retry behaviour) | ``OTHER`` (neither).

Conventions (per R-001): match STRUCTURED fields first, prose second.
Prose dates without a timezone are parsed conservatively: when the
reset moment is ambiguous the classifier returns ``reset_at=None``
rather than guessing.  Auth / billing errors are never quota.

Confidence: ``VERIFIED`` rules are quoted against real CLI captures in
R-001; ``PROVISIONAL`` rules (Cline, Antigravity failure text) have no
verified exact signal yet and are marked as such in code.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from saberops.models import QuotaObservation, QuotaRawSemantics, QuotaState

if TYPE_CHECKING:
    from saberops.control_plane.quota_policy import QuotaPoolPolicy
    from saberops.db import Database
else:  # pragma: no cover - typing only
    Database = Any  # type: ignore[assignment,misc]


class ExhaustionVerdict(StrEnum):
    """Outcome of plan-exhaustion classification."""

    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    RATE_LIMITED = "RATE_LIMITED"
    OTHER = "OTHER"


VERIFIED = "VERIFIED"
PROVISIONAL = "PROVISIONAL"


@dataclass(frozen=True)
class ExhaustionClassification:
    """Pure result of classifying one CLI execution's captured output.

    ``reset_at`` is an aware UTC datetime when the signal carries a reset
    moment (EXACT/PARSED/HINT), else ``None``.  ``rule`` names the exact
    R-001 table row that fired (for durable evidence); ``confidence``
    is ``VERIFIED`` or ``PROVISIONAL``.  ``window`` is
    ``5h|daily|weekly|monthly`` or ``None`` (unknown -> ALL);
    ``model_scope`` is a model substring or ``None``; ``reset_confidence``
    is ``EXACT|PARSED|HINT|UNKNOWN``.  Verdicts never change with these fields.
    """

    verdict: ExhaustionVerdict
    reset_at: datetime | None
    confidence: str
    rule: str
    window: str | None = None
    model_scope: str | None = None
    reset_confidence: str = "UNKNOWN"


def _utcnow(now: datetime | None) -> datetime:
    if now is not None:
        return now.astimezone(UTC) if now.tzinfo is not None else now.replace(
            tzinfo=UTC
        )
    return datetime.now(UTC)


def reset_to_iso(reset_at: datetime | None) -> str | None:
    """Render a reset datetime as canonical UTC ISO-8601, or ``None``."""
    if reset_at is None:
        return None
    aware = reset_at if reset_at.tzinfo is not None else reset_at.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


# ---------------------------------------------------------------------------
# Cross-cutting guards: auth / billing errors are NEVER quota.
# ---------------------------------------------------------------------------

_AUTH_RE = re.compile(
    r"not authenticated|unauthenticated|unauthorized|authentication (required|failed)"
    r"|invalid[ _-]?api[ _-]?key|api[ _-]?key (required|missing|invalid)"
    r"|login required|sign in|credentials|auth_required|subscription_inactive"
    r"|subscription inactive|unknown or disabled provider|permission denied",
    re.IGNORECASE,
)

# Billing / funds phrasing that must not become quota.  Bare "credits"
# is deliberately absent: real quota texts ("purchase more credits")
# mention credits, so only funds-specific phrasing guards here; the
# quota-specific rules below run first in every classifier anyway.
_BILLING_RE = re.compile(
    r"payment required|payment method|add (a )?payment|billing error|invoice"
    r"|top up|out of credits|insufficient credit|402\b|enforced_spend_limit_reached",
    re.IGNORECASE,
)

# NOTE: ``enforced_spend_limit_reached`` doubles as the Claude spend-cap
# marker (R-001: spend-cap 429 has no retry-after and needs human action,
# so it is OTHER, not self-resetting quota).  It lives in the billing
# guard so it can never become QUOTA_EXHAUSTED via prose matching.


def _json_lines(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


# ---------------------------------------------------------------------------
# Codex CLI -- ``codex exec --json`` (JSONL).
# ---------------------------------------------------------------------------

_CODEX_QUOTA_RE = re.compile(
    r"you('ve| have) hit your usage limit|usage limit (has been )?(reached|exceeded)",
    re.IGNORECASE,
)
_CODEX_RATE_RE = re.compile(
    r"rate limit reached for .* on tokens per min|rate_limit_exceeded"
    r"|please try again in [\d.]+s",
    re.IGNORECASE,
)
_CODEX_OTHER_RE = re.compile(r"at capacity|exceeded retry limit.*429", re.IGNORECASE)
_CODEX_RETRY_IN_RE = re.compile(r"please try again in ([\d.]+)s", re.IGNORECASE)
_CODEX_TRY_AGAIN_AT_RE = re.compile(
    r"try again at\s+(?P<date>[A-Z][a-z]{2,8}\s+\d{1,2}"
    r"(?:st|nd|rd|th)?,?\s+\d{4}\s+\d{1,2}:\d{2}\s*(?:AM|PM))",
    re.IGNORECASE,
)


def _parse_codex_hint_date(
    text: str, now: datetime | None = None, tz: Any | None = None
) -> datetime | None:
    """Parse Codex ``try again at <date>`` as HINT in host-local time.

    The prose ships no timezone; the next occurrence is read in host-local
    time (injectable ``tz`` for tests). Returns aware UTC or None.
    """
    match = _CODEX_TRY_AGAIN_AT_RE.search(text)
    if match is None:
        return None
    raw = match.group("date")
    cleaned = re.sub(r"(\d{1,2})(st|nd|rd|th)", r"\1", raw, flags=re.IGNORECASE)
    moment = _utcnow(now)
    try:
        local_tz = tz if tz is not None else datetime.now().astimezone().tzinfo
        naive = datetime.strptime(cleaned.strip(), "%b %d, %Y %I:%M %p")
    except ValueError:
        try:
            naive = datetime.strptime(cleaned.strip(), "%B %d, %Y %I:%M %p")
        except ValueError:
            return None
    try:
        assert local_tz is not None
        local_dt = naive.replace(tzinfo=local_tz)
        return local_dt.astimezone(UTC)
    except Exception:
        return None
    _ = moment


def classify_codex(
    stdout: str, stderr: str = "", *, now: datetime | None = None, tz: Any | None = None
) -> ExhaustionClassification:
    """Classify Codex ``exec --json`` output per the R-001 table (VERIFIED).

    Trap handled: ``Interrupted`` turns emit NO terminal event, and
    text-only errors carry no JSON event, so a stream without a
    ``turn.failed`` / top-level ``error`` event is ``OTHER`` -- stream
    end alone is never a limit.  The plan-limit prose reset date
    (``try again at <date>``) ships without a timezone: it is a HINT in
    host-local time (injectable ``tz`` for tests).
    """
    _ = stderr
    messages: list[str] = []
    saw_terminal_failure = False
    for event in _json_lines(stdout):
        event_type = event.get("type")
        if event_type == "turn.failed":
            error = event.get("error")
            text = error.get("message") if isinstance(error, dict) else None
            if isinstance(text, str) and text:
                messages.append(text)
                saw_terminal_failure = True
        elif event_type == "error":
            text = event.get("message")
            if isinstance(text, str) and text:
                messages.append(text)
                saw_terminal_failure = True
    if not saw_terminal_failure:
        return ExhaustionClassification(
            ExhaustionVerdict.OTHER, None, VERIFIED, "codex:no-terminal-event"
        )
    combined = "\n".join(messages)
    if _AUTH_RE.search(combined):
        return ExhaustionClassification(ExhaustionVerdict.OTHER, None, VERIFIED, "codex:auth")
    if _CODEX_QUOTA_RE.search(combined):
        # Prose reset ("try again at Oct 3rd, 2026 11:58 PM") has no
        # timezone: HINT in host-local time (injectable tz for tests).
        hint = _parse_codex_hint_date(combined, now=now, tz=tz)
        return ExhaustionClassification(
            ExhaustionVerdict.QUOTA_EXHAUSTED, hint, VERIFIED, "codex:usage-limit",
            window=None, model_scope=None,
            reset_confidence="HINT" if hint is not None else "UNKNOWN",
        )
    if _BILLING_RE.search(combined):
        return ExhaustionClassification(ExhaustionVerdict.OTHER, None, VERIFIED, "codex:billing")
    if _CODEX_RATE_RE.search(combined):
        reset: datetime | None = None
        match = _CODEX_RETRY_IN_RE.search(combined)
        if match:
            try:
                reset = _utcnow(now) + timedelta(seconds=float(match.group(1)))
            except ValueError:
                reset = None
        return ExhaustionClassification(
            ExhaustionVerdict.RATE_LIMITED, reset, VERIFIED, "codex:rate-limit"
        )
    if _CODEX_OTHER_RE.search(combined):
        return ExhaustionClassification(ExhaustionVerdict.OTHER, None, VERIFIED, "codex:capacity")
    return ExhaustionClassification(ExhaustionVerdict.OTHER, None, VERIFIED, "codex:other")


# ---------------------------------------------------------------------------
# Claude Code -- ``claude --print --output-format stream-json`` (NDJSON).
# ---------------------------------------------------------------------------

_CLAUDE_QUOTA_PROSE_RE = re.compile(
    r"you've hit your (session|weekly|opus|sonnet) limit|monthly spend limit"
    r"|weekly limit resets|session limit",
    re.IGNORECASE,
)
_CLAUDE_THROTTLE_RE = re.compile(
    r"server is temporarily limiting requests|overloaded|\b529\b", re.IGNORECASE
)
_CLAUDE_RATE_RE = re.compile(r"request rejected ?\(429\)", re.IGNORECASE)
# Prose reset with an explicit zone, e.g. "resets 10:20pm (Europe/Zurich)"
# or "your weekly limit resets Oct 10, 2am (UTC)".  Bare clock times
# ("resets 3:45pm") are ambiguous and intentionally NOT matched here.
_CLAUDE_ZONED_RESET_RE = re.compile(
    r"resets?\b(?P<body>[^.\n]*?)\((?P<zone>[A-Za-z_][A-Za-z0-9_+\-/]*)\)", re.IGNORECASE
)
_MONTH_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}"
    r"(?:st|nd|rd|th)?(?:,?\s+\d{4})?",
    re.IGNORECASE,
)
_MONTH_NAME_TO_NUM = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_CLOCK_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.IGNORECASE)


def _parse_claude_zoned_reset(body: str, zone: str, now: datetime) -> datetime | None:
    """Parse ``<month day[, year], clock>`` + IANA/UTC zone to UTC.

    Returns ``None`` for clock-only bodies (no calendar date): without
    a date the next occurrence is a guess, and this classifier does
    not guess.
    """
    month_match = _MONTH_RE.search(body)
    clock_match = _CLOCK_RE.search(body)
    if month_match is None or clock_match is None:
        return None
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("UTC" if zone.upper() == "UTC" else zone)
    except Exception:
        return None
    month_token = month_match.group(0)
    month_num = _MONTH_NAME_TO_NUM[month_token[:3].lower()]
    day_match = re.search(r"\d{1,2}", month_token[3:] or month_token)
    assert day_match is not None
    day = int(day_match.group(0))
    year_match = re.search(r"\d{4}", month_token)
    year = int(year_match.group(0)) if year_match else now.astimezone(tz).year
    hour = int(clock_match.group(1)) % 12
    if clock_match.group(3).lower() == "pm":
        hour += 12
    minute = int(clock_match.group(2)) if clock_match.group(2) else 0
    try:
        return datetime(year, month_num, day, hour, minute, tzinfo=tz).astimezone(UTC)
    except ValueError:
        return None


def _epoch_to_utc(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    seconds = float(value)
    # Guard against millisecond epochs: values far beyond any plausible
    # seconds epoch are treated as milliseconds.
    if seconds >= 1e12:
        seconds /= 1000.0
    if not 1e9 <= seconds < 1e11:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _parse_iso_to_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _parse_claude_bare_clock_hint(
    text: str, now: datetime, tz: Any | None = None
) -> datetime | None:
    """Parse a bare clock (``resets 3:45pm``) as HINT: next host-local occurrence.

    Time zone is injectable for tests; defaults to host-local.
    """
    match = _CLOCK_RE.search(text)
    if match is None:
        return None
    try:
        local_tz = tz if tz is not None else datetime.now().astimezone().tzinfo
        assert local_tz is not None
        local_now = now.astimezone(local_tz)
    except Exception:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).lower() == "pm":
        hour += 12
    minute = int(match.group(2)) if match.group(2) else 0
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate = candidate + timedelta(days=1)
    try:
        return candidate.astimezone(UTC)
    except Exception:
        return None


def _claude_window_and_scope(
    raw: str, rate_limit_type: str | None = None
) -> tuple[str | None, str | None]:
    """Window/model-scope from rateLimitType then prose (M2-1 B)."""
    rlt = (rate_limit_type or "").strip().lower()
    if rlt:
        scope: str | None = None
        if "opus" in rlt:
            scope = "opus"
        elif "sonnet" in rlt:
            scope = "sonnet"
        if "five_hour" in rlt or "5h" in rlt or "session" in rlt:
            return "5h", scope
        if "seven_day" in rlt or "weekly" in rlt or "7d" in rlt:
            return "weekly", scope
        if "daily" in rlt:
            return "daily", scope
        if "monthly" in rlt:
            return "monthly", scope
        if scope is not None:
            return "weekly", scope
    low = raw.lower()
    scope = None
    if "opus" in low:
        scope = "opus"
    elif "sonnet" in low:
        scope = "sonnet"
    if "session limit" in low:
        return "5h", scope
    if "weekly limit" in low:
        return "weekly", scope
    if scope is not None and "limit" in low:
        return "weekly", scope
    if "daily" in low and "limit" in low:
        return "daily", scope
    if "monthly" in low and "limit" in low:
        return "monthly", scope
    return None, scope


def classify_claude(
    stdout: str, stderr: str = "", *, now: datetime | None = None, tz: Any | None = None
) -> ExhaustionClassification:
    """Classify Claude ``stream-json`` output per the R-001 table (VERIFIED).

    Trap handled: a plan limit arrives as ``type=="result"`` with
    ``subtype=="success"`` AND ``is_error==true`` -- a classifier that
    treats ``success`` as success misses 100% of real limit events
    (R-001, rhei #451).  Reset extraction prefers structured
    ``resetsAt`` epoch seconds / ``resets_at`` ISO-8601 (EXACT); zoned or
    UTC-dated prose is PARSED; a bare clock (``resets 3:45pm``) is a HINT
    (next host-local occurrence, injectable ``tz``).
    """
    _ = stderr
    moment = _utcnow(now)
    events = _json_lines(stdout)
    raw = stdout
    result_events = [e for e in events if e.get("type") == "result"]
    assistant_frames = [e for e in events if e.get("type") == "assistant"]
    if _AUTH_RE.search(raw):
        return ExhaustionClassification(ExhaustionVerdict.OTHER, None, VERIFIED, "claude:auth")
    if "enforced_spend_limit_reached" in raw:
        # Spend-cap 429: no retry-after, needs human action -- OTHER.
        return ExhaustionClassification(ExhaustionVerdict.OTHER, None, VERIFIED, "claude:spend-cap")
    if _BILLING_RE.search(raw):
        return ExhaustionClassification(ExhaustionVerdict.OTHER, None, VERIFIED, "claude:billing")
    if _CLAUDE_THROTTLE_RE.search(raw):
        return ExhaustionClassification(ExhaustionVerdict.OTHER, None, VERIFIED, "claude:throttle")

    def _structured_reset() -> datetime | None:
        for frame in assistant_frames:
            api_error = frame.get("api_error")
            if isinstance(api_error, dict):
                reset = _epoch_to_utc(api_error.get("resetsAt"))
                if reset is not None:
                    return reset
            # Some frames carry resetsAt at the top level.
            reset = _epoch_to_utc(frame.get("resetsAt"))
            if reset is not None:
                return reset
        for event in events:
            if event.get("type") == "rate_limit_event":
                info = event.get("rate_limit_info")
                if isinstance(info, dict):
                    reset = _epoch_to_utc(info.get("resetsAt"))
                    if reset is not None:
                        return reset
            reset = _parse_iso_to_utc(event.get("resets_at"))
            if reset is not None:
                return reset
        return None

    def _prose_reset(text: str) -> datetime | None:
        match = _CLAUDE_ZONED_RESET_RE.search(text)
        if match is None:
            return None
        return _parse_claude_zoned_reset(match.group("body"), match.group("zone"), moment)

    has_usage_limit_marker = any(
        isinstance(f.get("api_error"), dict)
        and f["api_error"].get("api_error") == "usage_limit_reached"
        for f in assistant_frames
    ) or '"usage_limit_reached"' in raw
    quota_result = any(
        isinstance(e.get("subtype"), str)
        and e.get("subtype") == "success"
        and e.get("is_error") is True
        and (
            e.get("api_error_status") == 429
            or (isinstance(e.get("result"), str) and _CLAUDE_QUOTA_PROSE_RE.search(e["result"]))
        )
        for e in result_events
    )
    quota_prose = _CLAUDE_QUOTA_PROSE_RE.search(raw) is not None
    if has_usage_limit_marker or quota_result or (
        quota_prose
        and any(
            e.get("is_error") is True for e in result_events
        )
    ):
        # Window/model-scope: prefer rateLimitType, else limit prose only
        # (never the system init model name).
        rate_type: str | None = None
        for frame in assistant_frames:
            api_error = frame.get("api_error")
            if isinstance(api_error, dict) and isinstance(api_error.get("rateLimitType"), str):
                rate_type = api_error["rateLimitType"]
                break
        limit_texts: list[str] = []
        for event in result_events:
            result_text = event.get("result")
            if isinstance(result_text, str):
                limit_texts.append(result_text)
        for frame in assistant_frames:
            message = frame.get("message")
            if isinstance(message, dict) and isinstance(message.get("content"), list):
                for item in message["content"]:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        limit_texts.append(item["text"])
        window, scope = _claude_window_and_scope(" ".join(limit_texts), rate_type)
        reset = _structured_reset()
        reset_conf = "EXACT" if reset is not None else "UNKNOWN"
        prose_kind: str | None = None
        if reset is None:
            for event in result_events:
                result_text = event.get("result")
                if isinstance(result_text, str):
                    reset = _prose_reset(result_text)
                    if reset is not None:
                        prose_kind = "PARSED"
                        break
            if reset is None:
                for frame in assistant_frames:
                    message = frame.get("message")
                    if isinstance(message, dict) and isinstance(message.get("content"), list):
                        for item in message["content"]:
                            if isinstance(item, dict) and isinstance(item.get("text"), str):
                                reset = _prose_reset(item["text"])
                                if reset is not None:
                                    prose_kind = "PARSED"
                                    break
                        if reset is not None:
                            break
            if reset is not None:
                reset_conf = prose_kind or "PARSED"
            else:
                # Bare-clock HINT: next host-local occurrence.
                hint_text: str | None = None
                for event in result_events:
                    result_text = event.get("result")
                    if isinstance(result_text, str) and _CLOCK_RE.search(result_text):
                        hint_text = result_text
                        break
                if hint_text is None:
                    for frame in assistant_frames:
                        message = frame.get("message")
                        if isinstance(message, dict) and isinstance(message.get("content"), list):
                            for item in message["content"]:
                                if isinstance(item, dict) and isinstance(item.get("text"), str):
                                    if _CLOCK_RE.search(item["text"]):
                                        hint_text = item["text"]
                                        break
                            if hint_text is not None:
                                break
                if hint_text is not None:
                    hint = _parse_claude_bare_clock_hint(hint_text, moment, tz=tz)
                    if hint is not None:
                        reset, reset_conf = hint, "HINT"
        return ExhaustionClassification(
            ExhaustionVerdict.QUOTA_EXHAUSTED, reset, VERIFIED, "claude:usage-limit",
            window=window, model_scope=scope, reset_confidence=reset_conf,
        )
    # API per-minute over-use: retryable 429 surface.
    if _CLAUDE_RATE_RE.search(raw) or any(
        e.get("type") == "system" and e.get("subtype") == "api_retry" for e in events
    ):
        return ExhaustionClassification(
            ExhaustionVerdict.RATE_LIMITED, None, VERIFIED, "claude:rate-limit"
        )
    return ExhaustionClassification(ExhaustionVerdict.OTHER, None, VERIFIED, "claude:other")


# ---------------------------------------------------------------------------
# OpenCode -- ``opencode run --format json`` (JSONL) and serve SSE.
# ---------------------------------------------------------------------------

_OPENCODE_WINDOW_LIMIT_RE = re.compile(
    r"(weekly|monthly|5-hour|five-hour|hourly|daily)[^\n]{0,60}limit"
    r"|you have reached your[^\n]*limit|usage limit (has been )?(reached|exceeded)",
    re.IGNORECASE,
)
_OPENCODE_RATE_TEXT_RE = re.compile(r"rate limit exceeded|too many requests", re.IGNORECASE)
_OPENCODE_RESET_IN_RE = re.compile(
    r"resets?\s+in\s+(\d+)\s*(m|min|mins|minute|minutes|h|hour|hours|s|sec|secs|second|seconds)",
    re.IGNORECASE,
)
_OPENCODE_AUTH_RE = re.compile(
    r"auth_required|authentication|unauthorized|not authenticated|subscription_inactive"
    r"|subscription inactive|region_restricted",
    re.IGNORECASE,
)


def _window_from_text(text: str) -> str | None:
    """Window from vendor wording (M2-1 B); None when absent (=> ALL)."""
    low = text.lower()
    if re.search(r"5-hour|five-hour|five hour", low):
        return "5h"
    if "daily" in low:
        return "daily"
    if "weekly" in low:
        return "weekly"
    if "monthly" in low:
        return "monthly"
    return None


def classify_opencode(
    stdout: str, stderr: str = "", *, now: datetime | None = None
) -> ExhaustionClassification:
    """Classify OpenCode ``run --format json`` output per R-001 (VERIFIED).

    Trap handled: ``step_finish`` may never arrive (the run loop can
    strand the final event), so classification keys ONLY on the
    ``error`` event + message -- never on missing-step heuristics.
    ``Rate limit exceeded. Please try again later.`` is used for BOTH
    burst-rate and quota states, so text alone never separates them:
    named quota markers (``provider.quota`` / ``FreeUsageLimitError``
    / window nouns) decide.
    """
    _ = stderr
    quota_hit = False
    rate_hit = False
    reset_text: str | None = None
    saw_error_event = False
    for event in _json_lines(stdout):
        if event.get("type") != "error":
            continue
        saw_error_event = True
        event_status = event.get("status")
        if not isinstance(event_status, int):
            event_status = event.get("statusCode")
        error = event.get("error")
        blob = json.dumps(error) if error is not None else ""
        message = ""
        error_type: str | None = None
        status: int | None = None
        if isinstance(error, dict):
            error_type = error.get("type") if isinstance(error.get("type"), str) else None
            data = error.get("data")
            if isinstance(data, dict):
                raw_message = data.get("message")
                if isinstance(raw_message, str):
                    message += raw_message + "\n"
                raw_status = data.get("statusCode")
                if isinstance(raw_status, int):
                    status = raw_status
                nested = data.get("error")
                if isinstance(nested, dict):
                    nested_type = nested.get("type")
                    if isinstance(nested_type, str):
                        error_type = error_type or nested_type
            raw_message = error.get("message")
            if isinstance(raw_message, str):
                message += raw_message + "\n"
            for key in ("status", "statusCode"):
                raw_status = error.get(key)
                if isinstance(raw_status, int):
                    status = raw_status
            name = error.get("name")
            if isinstance(name, str):
                blob += "\n" + name
        if status is None and isinstance(event_status, int):
            status = event_status
        text = message + "\n" + blob
        if _OPENCODE_AUTH_RE.search(text) or _AUTH_RE.search(text):
            return ExhaustionClassification(
                ExhaustionVerdict.OTHER, None, VERIFIED, "opencode:auth"
            )
        if (
            error_type == "provider.quota"
            or "FreeUsageLimitError" in text
            or "QUOTA_EXCEEDED" in text
        ):
            quota_hit = True
            reset_text = (reset_text or "") + "\n" + text
        elif _OPENCODE_WINDOW_LIMIT_RE.search(text):
            quota_hit = True
            reset_text = (reset_text or "") + "\n" + text
        elif error_type == "provider.rate-limit":
            # Plan window only with ClinePass/Go/window wording;
            # otherwise a burst-rate 429.
            if _OPENCODE_WINDOW_LIMIT_RE.search(text) or re.search(
                r"clinepass|weekly|monthly|5-hour|go plus", text, re.IGNORECASE
            ):
                quota_hit = True
                reset_text = (reset_text or "") + "\n" + text
            else:
                rate_hit = True
        elif (status == 429 or status is None) and _OPENCODE_RATE_TEXT_RE.search(text):
            rate_hit = True
    if quota_hit:
        reset: datetime | None = None
        reset_conf = "UNKNOWN"
        if reset_text:
            match = _OPENCODE_RESET_IN_RE.search(reset_text)
            if match:
                amount = int(match.group(1))
                unit = match.group(2).lower()
                delta = (
                    timedelta(minutes=amount)
                    if unit.startswith("m")
                    else timedelta(hours=amount)
                    if unit.startswith("h")
                    else timedelta(seconds=amount)
                )
                reset = _utcnow(now) + delta
                reset_conf = "PARSED"
        window = _window_from_text(reset_text or "")
        return ExhaustionClassification(
            ExhaustionVerdict.QUOTA_EXHAUSTED, reset, VERIFIED, "opencode:quota",
            window=window, model_scope=None, reset_confidence=reset_conf,
        )
    if rate_hit:
        return ExhaustionClassification(
            ExhaustionVerdict.RATE_LIMITED, None, VERIFIED, "opencode:rate-limit"
        )
    if not saw_error_event:
        return ExhaustionClassification(
            ExhaustionVerdict.OTHER, None, VERIFIED, "opencode:no-error-event"
        )
    return ExhaustionClassification(ExhaustionVerdict.OTHER, None, VERIFIED, "opencode:other")


# ---------------------------------------------------------------------------
# Cline CLI -- ``cline --json`` (PROVISIONAL text-only rule).
# ---------------------------------------------------------------------------

# PROVISIONAL (R-001: no verified exact ClinePass-exhausted CLI signal
# exists; only the stderr error channel and agent_event error vocabulary
# are documented).  Text matching is the only lead and is fragile: the
# first live capture must confirm or revise this rule.
_CLINE_QUOTA_RE = re.compile(
    r"ClinePassLimitError|clinepass[^\n]*limit|weekly[^\n]*limit|5-hour[^\n]*limit"
    r"|five.hour[^\n]*limit|monthly[^\n]*limit|usage limit",
    re.IGNORECASE,
)
_CLINE_RATE_RE = re.compile(r"rate.?limit|too many requests|\b429\b", re.IGNORECASE)


def classify_cline(
    stdout: str, stderr: str = "", *, now: datetime | None = None
) -> ExhaustionClassification:
    """Classify Cline ``--json`` output (PROVISIONAL text-only rule).

    Channels: stderr ``{"type":"error","message":...}`` lines and
    ``agent_event`` / ``event.type=="error"`` payloads.  Reset format
    is UNKNOWN until verified, so the reset is always ``None``; prose
    resets are deliberately NOT parsed here.
    """
    _ = now
    texts: list[str] = []
    for event in _json_lines(stderr) + _json_lines(stdout):
        if event.get("type") == "error" and isinstance(event.get("message"), str):
            texts.append(event["message"])
        inner = event.get("event")
        if isinstance(inner, dict) and inner.get("type") == "error":
            texts.append(json.dumps(inner))
    combined = "\n".join(texts)
    if not combined.strip():
        return ExhaustionClassification(
            ExhaustionVerdict.OTHER, None, PROVISIONAL, "cline:no-error-signal"
        )
    if _AUTH_RE.search(combined):
        return ExhaustionClassification(ExhaustionVerdict.OTHER, None, PROVISIONAL, "cline:auth")
    if _BILLING_RE.search(combined):
        return ExhaustionClassification(ExhaustionVerdict.OTHER, None, PROVISIONAL, "cline:billing")
    if _CLINE_QUOTA_RE.search(combined):
        return ExhaustionClassification(
            ExhaustionVerdict.QUOTA_EXHAUSTED, None, PROVISIONAL, "cline:clinepass-window",
            window=_window_from_text(combined), model_scope=None, reset_confidence="UNKNOWN",
        )
    if _CLINE_RATE_RE.search(combined):
        return ExhaustionClassification(
            ExhaustionVerdict.RATE_LIMITED, None, PROVISIONAL, "cline:rate-limit"
        )
    return ExhaustionClassification(ExhaustionVerdict.OTHER, None, PROVISIONAL, "cline:other")


# ---------------------------------------------------------------------------
# Antigravity -- ``agy -p --output-format stream-json``.
# ---------------------------------------------------------------------------

_AGY_QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED|\b429\b|quota|exhausted", re.IGNORECASE)
_AGY_RATE_RE = re.compile(r"rate.?limit|too many requests|throttl", re.IGNORECASE)
_AGY_ERROR_LINE_RE = re.compile(r"AGY_ERROR:", re.IGNORECASE)


def parse_antigravity_usage(usage_json: str) -> datetime | None:
    """Read the reset clock from ``agy -p /usage --output-format json``.

    The failure signal itself carries no reset time (R-001); the
    zero-cost ``/usage`` command exposes per-bucket ``reset_time``
    (ISO-8601 UTC) + ``remaining_fraction``.  Returns the reset of the
    most-exhausted bucket (ties break to the earliest reset), or
    ``None`` when the payload is unparseable.  Shape VERIFIED by two
    independent captures (R-001).
    """
    try:
        payload = json.loads(usage_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    command = payload.get("command")
    data = command.get("data") if isinstance(command, dict) else None
    groups = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups, list):
        return None
    best: tuple[float, datetime] | None = None
    for group in groups:
        if not isinstance(group, dict):
            continue
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            continue
        for bucket in buckets:
            if not isinstance(bucket, dict):
                continue
            fraction = bucket.get("remaining_fraction")
            reset = _parse_iso_to_utc(bucket.get("reset_time"))
            if not isinstance(fraction, (int, float)) or isinstance(fraction, bool):
                continue
            if reset is None:
                continue
            candidate = (float(fraction), reset)
            if best is None or candidate < best:
                best = candidate
    return best[1] if best is not None else None


def classify_antigravity(
    stdout: str,
    stderr: str = "",
    *,
    usage_json: str | None = None,
    now: datetime | None = None,
) -> ExhaustionClassification:
    """Classify Antigravity ``stream-json`` output (PROVISIONAL text rule).

    Quota-exhausted turn: terminal ``{event:result,
    result:{status:"ERROR", error:"<text>"}}`` and/or an ``AGY_ERROR:``
    stderr line naming ``RESOURCE_EXHAUSTED`` / 429 / exhausted quota
    (INFERRED, single implementation source per R-001).  The failure
    signal carries no reset: pass the zero-cost ``/usage`` JSON via
    ``usage_json`` to resolve the reset clock (see
    :func:`parse_antigravity_usage`).
    """
    _ = now
    texts: list[str] = []
    has_error_shape = False
    for event in _json_lines(stdout):
        if event.get("event") != "result":
            continue
        result = event.get("result")
        if not isinstance(result, dict):
            continue
        if result.get("status") == "ERROR":
            has_error_shape = True
            error = result.get("error")
            if isinstance(error, str):
                texts.append(error)
            usage = result.get("usage")
            if usage is not None:
                texts.append(json.dumps(usage))
    if _AGY_ERROR_LINE_RE.search(stderr):
        has_error_shape = True
        texts.append(stderr)
    combined = "\n".join(texts)
    if not has_error_shape:
        return ExhaustionClassification(
            ExhaustionVerdict.OTHER, None, PROVISIONAL, "antigravity:no-error-result"
        )
    if _AUTH_RE.search(combined):
        return ExhaustionClassification(
            ExhaustionVerdict.OTHER, None, PROVISIONAL, "antigravity:auth"
        )
    if _BILLING_RE.search(combined):
        return ExhaustionClassification(
            ExhaustionVerdict.OTHER, None, PROVISIONAL, "antigravity:billing"
        )
    if _AGY_QUOTA_RE.search(combined):
        reset = parse_antigravity_usage(usage_json) if usage_json is not None else None
        return ExhaustionClassification(
            ExhaustionVerdict.QUOTA_EXHAUSTED, reset, PROVISIONAL, "antigravity:quota-text",
            window=_window_from_text(combined), model_scope=None,
            reset_confidence="EXACT" if reset is not None else "UNKNOWN",
        )
    if _AGY_RATE_RE.search(combined):
        return ExhaustionClassification(
            ExhaustionVerdict.RATE_LIMITED, None, PROVISIONAL, "antigravity:rate-limit"
        )
    return ExhaustionClassification(ExhaustionVerdict.OTHER, None, PROVISIONAL, "antigravity:other")


# ---------------------------------------------------------------------------
# Quota evidence: exhaustion observations may only EXCLUDE.
# ---------------------------------------------------------------------------

#: Default quota pool per worker provider for exhaustion evidence.
#: All five CLIs expose weekly plan windows (Codex weekly pool, Claude
#: weekly/session windows, OpenCode Go weekly bucket, ClinePass weekly
#: window, Antigravity weekly buckets); the finer window rides
#: ``window_type``.
DEFAULT_POOL = "weekly"

_PROVIDER_SOURCE = {
    "codex": "plan-exhaustion:codex",
    "anthropic": "plan-exhaustion:claude",
    "opencode": "plan-exhaustion:opencode",
    "cline": "plan-exhaustion:cline",
    "antigravity": "plan-exhaustion:antigravity",
}


def build_quota_observation(
    *,
    provider: str,
    pool: str = DEFAULT_POOL,
    window_type: str | None = None,
    reset_at: datetime | None = None,
    source: str | None = None,
    confidence: str = VERIFIED,
    run_id: str | None = None,
    observed_at: str | None = None,
) -> QuotaObservation:
    """Build an exhaustion observation: remaining 0, freshness FRESH.

    The observation is purely exclusionary: ``raw_value=0`` remaining
    percent normalizes to 0, which can only demote/block the pool in
    every routing consumer (legacy ``derive_routing_state`` and
    control-plane ``quota_decision``), never enable a route.
    """
    from saberops.db import current_iso_timestamp

    return QuotaObservation(
        id=f"exh_{uuid.uuid4().hex[:12]}",
        provider=provider.strip().lower(),
        account_ref=None,
        pool=pool.strip().lower(),
        window_type=window_type,
        raw_value=0.0,
        raw_limit=None,
        raw_semantics=QuotaRawSemantics.REMAINING_PERCENT.value,
        normalized_remaining_pct=0.0,
        reset_at=reset_to_iso(reset_at),
        observed_at=observed_at or current_iso_timestamp(),
        source=source
        or _PROVIDER_SOURCE.get(provider.strip().lower(), f"plan-exhaustion:{provider}"),
        freshness="FRESH",
        confidence=confidence,
        run_id=run_id,
    )


def record_exhaustion(
    db: Database,
    *,
    provider: str,
    pool: str = DEFAULT_POOL,
    window_type: str | None = None,
    reset_at: datetime | None = None,
    run_id: str | None = None,
    source: str | None = None,
    confidence: str = VERIFIED,
    model: str | None = None,
    window: str | None = None,
    model_scope: str | None = None,
    reset_confidence: str = "UNKNOWN",
    pool_policies: dict[str, QuotaPoolPolicy] | None = None,
    pools: list[str] | None = None,
) -> QuotaState:
    """Persist exhaustion evidence and upsert pool exclusions (M2-1 B).

    Writes the quota observation (audit) and then upserts an exclusion for
    every pool the candidate draws (D). It no longer calls
    ``db.set_quota(..., 0.0, ...)``. The observation is purely exclusionary
    and never synthesizes, enables, or fails over to any other route (in
    particular never to an API-key / METERED_KEY route such as OmniRoute
    or direct_api). Returns a synthesized remaining-0 ``QuotaState`` for
    caller convenience (routing NORMAL; exclusion handled via the seam).
    """
    from saberops.models import RoutingState

    window_id = (window or window_type or pool or "ALL").strip().lower() or "ALL"
    # Normalize window vocabulary.
    alias = {"five_hour": "5h", "five-hour": "5h", "session": "5h", "5-hour": "5h"}
    window_id = alias.get(window_id, window_id)
    if window_id not in ("5h", "daily", "weekly", "monthly", "ALL"):
        # ``pool`` legacy values are windows; unknown -> ALL only when no
        # explicit window was given.
        window_id = window_id if (window or window_type) else "ALL"
    observation = build_quota_observation(
        provider=provider,
        pool=window_id,
        window_type=window_id,
        reset_at=reset_at,
        source=source,
        confidence=confidence,
        run_id=run_id,
    )
    if run_id is not None:
        try:
            db.create_run_quota_observation(run_id, observation)
        except Exception:
            db.create_quota_observation(observation)
    else:
        db.create_quota_observation(observation)
    # Pools the candidate draws.
    pool_ids: list[str]
    if pools is not None:
        pool_ids = [p for p in pools if p]
    elif pool_policies is not None:
        try:
            from saberops.control_plane.quota_policy import pools_for as _pools_for
            from saberops.models import WorkerCandidate

            pool_ids = _pools_for(
                WorkerCandidate(provider=provider, model=model or ""), pool_policies
            )
        except Exception:
            pool_ids = [provider.strip().lower()]
    else:
        pool_ids = [provider.strip().lower()]
    if not pool_ids:
        pool_ids = [provider.strip().lower()]
    from saberops.capacity.store import upsert_exclusion

    conf = (reset_confidence or "UNKNOWN").strip().upper()
    if conf not in ("EXACT", "PARSED", "HINT", "UNKNOWN"):
        conf = "UNKNOWN"
    harness = (source or "").split(":")[-1] if source else provider.strip().lower()
    first_id: str | None = None
    for pool_id in pool_ids:
        try:
            row = upsert_exclusion(
                db, pool_id=pool_id, window_id=window_id,
                reason="QUOTA_EXHAUSTED", until=reset_at,
                until_confidence=conf, source=harness or provider.strip().lower(),
                model_scope=model_scope, evidence_ref=observation.id,
                run_id=run_id,
            )
            if first_id is None:
                first_id = str(row.get("id") or "")
        except Exception:
            continue
    from saberops.db import current_iso_timestamp

    return QuotaState(
        provider=harness or provider.strip().lower(),
        pool=window_id,
        remaining_percent=0.0,
        reset_at=reset_to_iso(reset_at),
        routing_state=RoutingState.NORMAL,
        updated_at=current_iso_timestamp(),
        pool_id=pool_ids[0],
        normalized_remaining_pct=0.0,
        freshness="FRESH",
        exclusion_id=first_id or observation.id,
        exclusion_until=reset_to_iso(reset_at),
        exclusion_confidence=conf,
        exclusion_model_scope=model_scope,
    )


__all__ = [
    "DEFAULT_POOL",
    "PROVISIONAL",
    "VERIFIED",
    "ExhaustionClassification",
    "ExhaustionVerdict",
    "build_quota_observation",
    "classify_antigravity",
    "classify_claude",
    "classify_cline",
    "classify_codex",
    "classify_opencode",
    "parse_antigravity_usage",
    "record_exhaustion",
    "reset_to_iso",
]
