"""Switch the owner mode from the owner's typed chat message (ADR-0090).

The owner mode is the one setting that decides what the engine may do
unattended (ADR-0082).  Three surfaces change it, and only these three:

1. **A typed chat command through a CLI hook.**  ``/saberops-away 2h``,
   ``/saberops-critical-only`` and ``/saberops-ask-me`` are handled by
   :func:`run_hook`, which runs from the CLI's *owner prompt* hook (Claude
   Code's ``UserPromptSubmit`` is the only one this repository documents).
   The handler accepts a payload only when the CLI says the event is the
   owner's typed prompt: tool output, a file the AI read, or any other event
   is refused, so text the AI reads cannot switch to ``away``.
2. **A terminal confirmation code.**  Anything looser than the current mode
   proposed from a chat without that hook needs a short code that is printed
   only on the owner's terminal (:func:`request_mode_change`) and consumed by
   :func:`confirm_mode_change`.  The request command refuses to reveal the
   code when stdin is not a terminal, so an agent-run shell never sees it.
3. **The owner's terminal.**  ``orch mode set`` (the plain owner command) and
   the local Questions page stay as they are.

The planner over MCP may only *tighten*: :func:`tighten_from_planner` switches
to ``ask-me`` and nothing else.  A loosening stays an owner question
(``propose_mode_change``); no MCP tool sets ``critical-only`` or ``away``.

Every applied switch is recorded twice: the mode document keeps the path in
``set_by`` (``owner-via-claude-hook``, ``owner-confirmation``,
``planner:tighten-mode``, ``owner``), and one JSON line is appended to the
mode-switch log so an ignored, refused or applied attempt is auditable.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from saberops.owner_mode import (
    OwnerMode,
    OwnerModeError,
    OwnerModeSettings,
    load_effective_owner_mode,
    load_owner_mode_settings,
    mode_view,
    normalize_until,
    parse_owner_mode,
    set_owner_mode,
)
from saberops.paths import (
    get_mode_confirmation_path,
    get_mode_switch_log_path,
)

__all__ = [
    "CONFIRMATION_CODE_LENGTH",
    "CONFIRMATION_TTL",
    "COMMAND_PREFIX",
    "HOOK_SUPPORT",
    "HookOutcome",
    "HookSupport",
    "ModeCommand",
    "ModeSwitchError",
    "PendingModeChange",
    "confirm_mode_change",
    "describe_hook_support",
    "hook_support",
    "install_mode_hook",
    "is_tightening",
    "parse_mode_command",
    "record_mode_switch",
    "remove_mode_hook",
    "request_mode_change",
    "run_hook",
    "tighten_from_planner",
]

_LOGGER = logging.getLogger(__name__)

#: Every typed command starts with this; anything else is an ordinary prompt.
COMMAND_PREFIX: Final[str] = "/saberops-"

_AWAY_COMMAND: Final[str] = "/saberops-away"
_CRITICAL_ONLY_COMMAND: Final[str] = "/saberops-critical-only"
_ASK_ME_COMMAND: Final[str] = "/saberops-ask-me"

_DURATION_RE: Final[re.Pattern[str]] = re.compile(r"^(\d{1,5})([mhd])$")
_UNIT_KEYWORD: Final[dict[str, str]] = {"m": "minutes", "h": "hours", "d": "days"}
_MIN_DURATION: Final[timedelta] = timedelta(minutes=1)
_MAX_DURATION: Final[timedelta] = timedelta(days=30)

CONFIRMATION_CODE_LENGTH: Final[int] = 6
#: Unambiguous characters: no 0/O or 1/I to mistype from a terminal.
_CODE_ALPHABET: Final[str] = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CONFIRMATION_TTL: Final[timedelta] = timedelta(minutes=15)
_CONFIRMATION_VERSION: Final[int] = 1

#: Claude Code's user settings document, relative to the home directory.
CLAUDE_SETTINGS_FILE: Final[str] = ".claude/settings.json"


class ModeSwitchError(ValueError):
    """A typed command, hook payload or confirmation that cannot be applied."""


# --------------------------------------------------------------------------- #
# Which CLIs have an owner-prompt hook
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HookSupport:
    """One CLI's owner-prompt hook, as this repository can document it."""

    cli: str
    supported: bool
    event: str | None
    prompt_field: str
    reason: str


#: The four planner CLIs.  Only Claude Code documents a hook on the owner's
#: typed message that this repository can rely on; the entry is unverified on a
#: live CLI here (H37 does the real-session check, owner present).  Codex is
#: explicitly unsupported because SaberOps itself launches it with
#: ``-c features.hooks=false`` (ADR-0019), so an installed hook would never run.
HOOK_SUPPORT: Final[tuple[HookSupport, ...]] = (
    HookSupport(
        "claude",
        True,
        "UserPromptSubmit",
        "prompt",
        "Claude Code's UserPromptSubmit hook receives the owner's typed prompt "
        "and is the only documented owner-prompt hook here; unverified on a "
        "live CLI in this lane.",
    ),
    HookSupport(
        "codex",
        False,
        None,
        "prompt",
        "no owner-prompt hook: SaberOps launches Codex with features.hooks=false "
        "(ADR-0019), so a hook would never run. Use the confirmation code.",
    ),
    HookSupport(
        "opencode",
        False,
        None,
        "prompt",
        "no owner-prompt hook is documented in this repository; use the "
        "confirmation code until H37 verifies a plugin message event.",
    ),
    HookSupport(
        "antigravity",
        False,
        None,
        "prompt",
        "Antigravity documents no owner-prompt hook; use the confirmation code.",
    ),
)


def hook_support(cli: str) -> HookSupport | None:
    """Return one CLI's hook support, or ``None`` for an unknown CLI."""
    wanted = cli.strip().lower()
    return next((row for row in HOOK_SUPPORT if row.cli == wanted), None)


# --------------------------------------------------------------------------- #
# The typed command
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ModeCommand:
    """One parsed typed command: the mode plus its away bounds."""

    raw: str
    mode: OwnerMode
    until: str | None = None
    max_jobs: int | None = None


def _utc_now(now: datetime | None) -> datetime:
    current = now if now is not None else datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _instant(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_duration(token: str, *, now: datetime) -> str:
    match = _DURATION_RE.match(token)
    if match is None:
        raise ModeSwitchError(
            "the away duration must look like `30m`, `2h` or `1d`"
        )
    amount = int(match.group(1))
    delta = timedelta(**{_UNIT_KEYWORD[match.group(2)]: amount})
    if delta < _MIN_DURATION or delta > _MAX_DURATION:
        raise ModeSwitchError("the away duration must be between 1 minute and 30 days")
    return _instant(now + delta)


def _parse_job_budget(token: str) -> int:
    if not token.isdigit() or int(token) < 1:
        raise ModeSwitchError("--max-jobs needs a positive whole number")
    return int(token)


def parse_mode_command(
    text: object, *, now: datetime | None = None
) -> ModeCommand | None:
    """Parse one typed mode command, or return ``None`` for an ordinary prompt.

    The command must be the *whole* prompt and one line.  A document the owner
    pasted, or a sentence that merely mentions the command, is not applied:
    the engine must not act on text that only looks like a command.
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped.startswith(COMMAND_PREFIX):
        return None
    if "\n" in stripped or "\r" in stripped:
        raise ModeSwitchError("a mode command must be exactly one line, alone in the message")
    parts = stripped.split()
    word = parts[0]
    if word == _ASK_ME_COMMAND:
        if len(parts) != 1:
            raise ModeSwitchError(f"`{_ASK_ME_COMMAND}` takes no arguments")
        return ModeCommand(stripped, OwnerMode.ASK_ME)
    if word == _CRITICAL_ONLY_COMMAND:
        if len(parts) != 1:
            raise ModeSwitchError(f"`{_CRITICAL_ONLY_COMMAND}` takes no arguments")
        return ModeCommand(stripped, OwnerMode.CRITICAL_ONLY)
    if word == _AWAY_COMMAND:
        duration: str | None = None
        max_jobs: int | None = None
        tokens = parts[1:]
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token == "--max-jobs":
                if index + 1 >= len(tokens):
                    raise ModeSwitchError("--max-jobs needs a whole number")
                max_jobs = _parse_job_budget(tokens[index + 1])
                index += 2
                continue
            if duration is None:
                duration = token
                index += 1
                continue
            raise ModeSwitchError(
                f"unexpected argument after `{_AWAY_COMMAND}`; use a duration "
                "and optionally --max-jobs"
            )
        if duration is None:
            raise ModeSwitchError(
                f"`{_AWAY_COMMAND}` needs a duration such as `2h`; an unbounded "
                "away session is refused"
            )
        return ModeCommand(
            stripped,
            OwnerMode.AWAY,
            until=_parse_duration(duration, now=_utc_now(now)),
            max_jobs=max_jobs,
        )
    known = ", ".join((_AWAY_COMMAND, _CRITICAL_ONLY_COMMAND, _ASK_ME_COMMAND))
    raise ModeSwitchError(f"unknown SaberOps command; known commands: {known}")


# --------------------------------------------------------------------------- #
# The switch log
# --------------------------------------------------------------------------- #


def record_mode_switch(
    *,
    source: str,
    outcome: str,
    mode: str | None = None,
    until: str | None = None,
    max_jobs: int | None = None,
    detail: str = "",
    now: datetime | None = None,
    path: Path | None = None,
) -> None:
    """Append one line to the mode-switch log; never fail the switch itself.

    The log records *how* a switch happened and its outcome.  It never stores
    the prompt text, so a pasted secret is not copied into the audit trail.
    """
    target = path if path is not None else get_mode_switch_log_path()
    line = json.dumps(
        {
            "at": _instant(_utc_now(now)),
            "source": source,
            "outcome": outcome,
            "mode": mode,
            "until": until,
            "max_jobs": max_jobs,
            "detail": detail,
        },
        sort_keys=True,
    )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:  # the mode document already carries the same fact
        _LOGGER.warning("mode-switch log write failed: %s", exc)


# --------------------------------------------------------------------------- #
# The hook handler
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HookOutcome:
    """What one hook payload did, for the CLI response and the log."""

    status: str  # applied | ignored | refused | unsupported
    cli: str
    detail: str
    mode: OwnerMode | None = None
    until: str | None = None
    max_jobs: int | None = None

    @property
    def changed(self) -> bool:
        """True only when the persisted owner mode changed."""
        return self.status == "applied"


def _refuse(cli: str, detail: str, *, source: str, now: datetime | None) -> HookOutcome:
    record_mode_switch(source=source, outcome="refused", detail=detail, now=now)
    return HookOutcome("refused", cli, detail)


def run_hook(cli: str, payload: object, *, now: datetime | None = None) -> HookOutcome:
    """Handle one owner-prompt hook payload; never raise to the hook host.

    The payload is accepted only when the CLI names its own *owner prompt*
    event.  A tool-output event (``PostToolUse`` and friends), an unknown
    event or anything that is not the CLI's prompt field is refused: text the
    AI reads cannot switch the mode.
    """
    source = f"hook:{cli}"
    support = hook_support(cli)
    if support is None:
        return _refuse(
            cli,
            f"unknown CLI {cli!r}; no mode hook handles it",
            source=source,
            now=now,
        )
    if not support.supported or support.event is None:
        record_mode_switch(
            source=source, outcome="unsupported", detail=support.reason, now=now
        )
        return HookOutcome("unsupported", cli, support.reason)
    if not isinstance(payload, Mapping):
        return _refuse(
            cli, "the hook payload is not a JSON object", source=source, now=now
        )
    event = payload.get("hook_event_name")
    if event != support.event:
        return _refuse(
            cli,
            "the payload is not the owner's typed prompt event "
            f"({support.event}); the owner mode is unchanged",
            source=source,
            now=now,
        )
    prompt = payload.get(support.prompt_field)
    if not isinstance(prompt, str):
        return _refuse(
            cli,
            f"the payload has no {support.prompt_field!r} text",
            source=source,
            now=now,
        )
    try:
        command = parse_mode_command(prompt, now=now)
    except ModeSwitchError as exc:
        return _refuse(cli, str(exc), source=source, now=now)
    if command is None:
        return HookOutcome("ignored", cli, "not a SaberOps mode command")
    try:
        set_owner_mode(
            command.mode,
            until=command.until,
            max_jobs=command.max_jobs,
            set_by=f"owner-via-{cli}-hook",
        )
    except (OwnerModeError, OSError) as exc:
        return _refuse(
            cli,
            f"the owner mode could not be saved: {exc}",
            source=source,
            now=now,
        )
    detail = f"owner mode set to '{command.mode.value}'"
    if command.until:
        detail += f" until {command.until}"
    if command.max_jobs is not None:
        detail += f", up to {command.max_jobs} job(s)"
    detail += f" by the owner's typed command ({cli} hook)"
    record_mode_switch(
        source=source,
        outcome="applied",
        mode=command.mode.value,
        until=command.until,
        max_jobs=command.max_jobs,
        detail=detail,
        now=now,
    )
    return HookOutcome(
        "applied",
        cli,
        detail,
        mode=command.mode,
        until=command.until,
        max_jobs=command.max_jobs,
    )


# --------------------------------------------------------------------------- #
# Tightening from the planner (MCP)
# --------------------------------------------------------------------------- #


def is_tightening(target: OwnerMode, current: OwnerMode) -> bool:
    """True only for the one switch that cannot widen any permission.

    ``ask-me`` carries the narrowest ceiling.  ``critical-only`` and ``away``
    share the unattended ceiling today, so a switch between them cannot be
    proven narrower and is treated as a loosening.
    """
    return target is OwnerMode.ASK_ME and current is not OwnerMode.ASK_ME


def tighten_from_planner(
    *, now: datetime | None = None
) -> tuple[dict[str, Any], bool]:
    """Switch to ``ask-me`` from a planner; return ``(mode view, changed)``.

    This is the only mode change the planner may make directly over MCP.  It
    only ever removes permission, so it is always safe to apply; every other
    mode is an owner decision.
    """
    current = load_effective_owner_mode(now=now)
    if not is_tightening(OwnerMode.ASK_ME, current):
        return mode_view(now=now), False
    set_owner_mode(OwnerMode.ASK_ME, set_by="planner:tighten-mode")
    record_mode_switch(
        source="planner", outcome="applied", mode=OwnerMode.ASK_ME.value,
        detail="the planning session tightened the owner mode to ask-me",
        now=now,
    )
    return mode_view(now=now), True


# --------------------------------------------------------------------------- #
# The confirmation code for a loosening without a hook
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PendingModeChange:
    """One pending loosening, waiting for its terminal confirmation code."""

    code: str
    mode: OwnerMode
    until: str | None
    max_jobs: int | None
    requested_by: str
    created_at: str
    expires_at: str


def _write_pending(pending: PendingModeChange, path: Path) -> None:
    payload = {
        "version": _CONFIRMATION_VERSION,
        "code": pending.code,
        "mode": pending.mode.value,
        "until": pending.until,
        "max_jobs": pending.max_jobs,
        "requested_by": pending.requested_by,
        "created_at": pending.created_at,
        "expires_at": pending.expires_at,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _read_pending(path: Path) -> PendingModeChange | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModeSwitchError(f"the pending mode change cannot be read: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != _CONFIRMATION_VERSION:
        raise ModeSwitchError("the pending mode change is malformed; request it again")
    try:
        mode = parse_owner_mode(data.get("mode"))
        code = data.get("code")
        created_at = data.get("created_at")
        expires_at = data.get("expires_at")
        requested_by = data.get("requested_by")
        until = data.get("until")
        max_jobs = data.get("max_jobs")
    except OwnerModeError as exc:
        raise ModeSwitchError(str(exc)) from exc
    if not isinstance(code, str) or not isinstance(created_at, str):
        raise ModeSwitchError("the pending mode change is malformed; request it again")
    if not isinstance(expires_at, str) or not isinstance(requested_by, str):
        raise ModeSwitchError("the pending mode change is malformed; request it again")
    return PendingModeChange(
        code=code,
        mode=mode,
        until=until if isinstance(until, str) else None,
        max_jobs=(
            max_jobs
            if isinstance(max_jobs, int) and not isinstance(max_jobs, bool)
            else None
        ),
        requested_by=requested_by,
        created_at=created_at,
        expires_at=expires_at,
    )


def _new_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CONFIRMATION_CODE_LENGTH))


def request_mode_change(
    mode: OwnerMode | str,
    *,
    until: str | None = None,
    max_jobs: int | None = None,
    requested_by: str = "owner",
    now: datetime | None = None,
    path: Path | None = None,
) -> PendingModeChange:
    """Create one pending loosening and return it *including its code*.

    Only the terminal command prints the code (see ``orch mode request``), so
    a chat or a tool result never carries it.
    """
    current_time = _utc_now(now)
    resolved = mode if isinstance(mode, OwnerMode) else parse_owner_mode(mode)
    end = normalize_until(until)
    if end is not None and resolved is not OwnerMode.AWAY:
        raise ModeSwitchError("an end time is only valid with away")
    if isinstance(max_jobs, bool) or (max_jobs is not None and (
        not isinstance(max_jobs, int) or max_jobs < 1
    )):
        raise ModeSwitchError("a job budget must be a positive whole number")
    if max_jobs is not None and resolved is not OwnerMode.AWAY:
        raise ModeSwitchError("a job budget is only valid with away")
    if resolved is OwnerMode.AWAY and end is None and max_jobs is None:
        raise ModeSwitchError(
            "away needs an end time or a job budget; an unbounded away session is refused"
        )
    if not isinstance(requested_by, str) or not requested_by.strip():
        raise ModeSwitchError("requested_by must be non-empty text")
    pending = PendingModeChange(
        code=_new_code(),
        mode=resolved,
        until=end,
        max_jobs=max_jobs,
        requested_by=requested_by.strip(),
        created_at=_instant(current_time),
        expires_at=_instant(current_time + CONFIRMATION_TTL),
    )
    target = path if path is not None else get_mode_confirmation_path()
    _write_pending(pending, target)
    record_mode_switch(
        source="confirmation-request",
        outcome="pending",
        mode=resolved.value,
        until=end,
        max_jobs=max_jobs,
        detail=f"a {CONFIRMATION_TTL.total_seconds() / 60:.0f}-minute confirmation "
        "code was written to the owner's terminal store",
        now=now,
    )
    return pending


def confirm_mode_change(
    code: str, *, now: datetime | None = None, path: Path | None = None
) -> OwnerModeSettings:
    """Apply the pending loosening when the short code matches, once.

    A missing, expired, malformed or wrong code changes nothing.  A correct
    code applies exactly the stored change and removes it, so the code cannot
    be replayed.
    """
    target = path if path is not None else get_mode_confirmation_path()
    pending = _read_pending(target)
    if pending is None:
        raise ModeSwitchError(
            "no pending mode change; the owner runs `orch mode request <mode>` first"
        )
    current_time = _utc_now(now)
    try:
        expires = datetime.fromisoformat(pending.expires_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModeSwitchError(
            "the pending mode change is malformed; request it again"
        ) from exc
    if expires <= current_time:
        target.unlink(missing_ok=True)
        raise ModeSwitchError(
            "the confirmation code expired; the owner requests the change again"
        )
    supplied = code.strip().upper()
    if not secrets.compare_digest(supplied, pending.code):
        record_mode_switch(
            source="confirmation",
            outcome="refused",
            mode=pending.mode.value,
            detail="a confirmation code did not match; nothing changed",
            now=now,
        )
        raise ModeSwitchError("the confirmation code does not match the pending change")
    set_owner_mode(
        pending.mode,
        until=pending.until,
        max_jobs=pending.max_jobs,
        set_by="owner-confirmation",
    )
    target.unlink(missing_ok=True)
    record_mode_switch(
        source="confirmation",
        outcome="applied",
        mode=pending.mode.value,
        until=pending.until,
        max_jobs=pending.max_jobs,
        detail="the owner confirmed a looser mode with the terminal code",
        now=now,
    )
    return load_owner_mode_settings()


# --------------------------------------------------------------------------- #
# Installing the Claude Code hook
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HookInstallResult:
    """One hook install or removal and what happened to the settings file."""

    cli: str
    path: str
    action: str  # installed | unchanged | removed | absent | unsupported | refused
    message: str
    ok: bool = True


def _claude_settings_path(home: Path | None) -> Path:
    base = home if home is not None else Path.home()
    return base / CLAUDE_SETTINGS_FILE


def _command_suffix(cli: str) -> str:
    return f" mode hook run --cli {cli}"


def _hook_command(cli: str, orch: str | None) -> str:
    if orch is None:
        from saberops.agent_connect import orch_command

        orch = orch_command()
    return f"{shlex.quote(orch)}{_command_suffix(cli)}"


def _is_our_command(value: object, cli: str) -> bool:
    return isinstance(value, str) and _command_suffix(cli) in value


def _has_our_hook(entries: list[Any], cli: str) -> bool:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        inner = entry.get("hooks")
        if not isinstance(inner, list):
            continue
        if any(
            isinstance(hook, dict) and _is_our_command(hook.get("command"), cli)
            for hook in inner
        ):
            return True
    return False


def _read_settings(path: Path, cli: str) -> dict[str, Any] | HookInstallResult:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return HookInstallResult(
            cli,
            str(path),
            "refused",
            f"{path} is not plain JSON, so it was left untouched; add the hook by hand.",
            ok=False,
        )
    if not isinstance(data, dict):
        return HookInstallResult(
            cli, str(path), "refused", f"{path} is not a JSON object; left untouched.", ok=False
        )
    return data


def _write_settings(path: Path, data: dict[str, Any]) -> None:
    if path.is_file():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.bak-saberops-{stamp}")
        backup.write_bytes(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def install_mode_hook(
    cli: str,
    *,
    home: Path | None = None,
    orch: str | None = None,
    dry_run: bool = False,
) -> HookInstallResult:
    """Merge SaberOps's owner-prompt hook into the CLI's settings file.

    Only Claude Code is writable today.  Every other key and hook entry in the
    file is preserved; a file that is not plain JSON is never rewritten.
    """
    support = hook_support(cli)
    if support is None or not support.supported or support.event is None:
        reason = support.reason if support is not None else f"unknown CLI {cli!r}"
        return HookInstallResult(cli, "", "unsupported", reason)
    path = _claude_settings_path(home)
    loaded = _read_settings(path, cli)
    if isinstance(loaded, HookInstallResult):
        return loaded
    data = loaded
    hooks = data.get("hooks")
    if hooks is None:
        hooks = {}
    if not isinstance(hooks, dict):
        return HookInstallResult(
            cli, str(path), "refused", f"{path}: 'hooks' is not a JSON object; left untouched.",
            ok=False,
        )
    entries = hooks.get(support.event)
    if entries is None:
        entries = []
    if not isinstance(entries, list):
        return HookInstallResult(
            cli,
            str(path),
            "refused",
            f"{path}: 'hooks.{support.event}' is not a list; left untouched.",
            ok=False,
        )
    if _has_our_hook(entries, cli):
        return HookInstallResult(
            cli, str(path), "unchanged", f"{support.cli} already runs the SaberOps mode hook."
        )
    command = _hook_command(cli, orch)
    entries.append({"hooks": [{"type": "command", "command": command}]})
    hooks[support.event] = entries
    data["hooks"] = hooks
    if dry_run:
        return HookInstallResult(
            cli, str(path), "would-install", f"Would run the mode hook on {support.cli}."
        )
    try:
        _write_settings(path, data)
    except OSError as exc:
        return HookInstallResult(
            cli, str(path), "refused", f"{path} could not be written: {exc}", ok=False
        )
    return HookInstallResult(
        cli,
        str(path),
        "installed",
        f"{support.cli} runs `{command}` on the owner's typed prompt.",
    )


def remove_mode_hook(cli: str, *, home: Path | None = None) -> HookInstallResult:
    """Remove only SaberOps's own hook entry; every other setting stays."""
    support = hook_support(cli)
    if support is None:
        return HookInstallResult(cli, "", "unsupported", f"unknown CLI {cli!r}")
    path = _claude_settings_path(home)
    loaded = _read_settings(path, cli)
    if isinstance(loaded, HookInstallResult):
        return loaded
    data = loaded
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return HookInstallResult(cli, str(path), "absent", "No SaberOps mode hook is installed.")
    event = support.event or ""
    entries = hooks.get(event)
    if not isinstance(entries, list):
        return HookInstallResult(cli, str(path), "absent", "No SaberOps mode hook is installed.")
    kept: list[Any] = []
    removed = False
    for entry in entries:
        if isinstance(entry, dict):
            inner = entry.get("hooks")
            if isinstance(inner, list):
                remaining = [
                    hook
                    for hook in inner
                    if not (
                        isinstance(hook, dict)
                        and _is_our_command(hook.get("command"), cli)
                    )
                ]
                if remaining != inner:
                    removed = True
                    if remaining:
                        entry = {**entry, "hooks": remaining}
                    else:
                        continue
        kept.append(entry)
    if not removed:
        return HookInstallResult(cli, str(path), "absent", "No SaberOps mode hook is installed.")
    if kept:
        hooks[event] = kept
    else:
        hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    try:
        _write_settings(path, data)
    except OSError as exc:
        return HookInstallResult(
            cli, str(path), "refused", f"{path} could not be written: {exc}", ok=False
        )
    return HookInstallResult(
        cli, str(path), "removed", f"Removed the SaberOps mode hook from {support.cli}."
    )


def describe_hook_support() -> list[dict[str, object]]:
    """Return the per-CLI support table for a report or CLI listing."""
    return [
        {
            "cli": row.cli,
            "supported": row.supported,
            "event": row.event,
            "prompt_field": row.prompt_field,
            "reason": row.reason,
        }
        for row in HOOK_SUPPORT
    ]
