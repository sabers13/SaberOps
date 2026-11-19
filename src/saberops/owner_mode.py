"""One persisted owner mode, and the narrowing decision every action consults.

The owner mode answers a different question from the authority presets in
:mod:`saberops.authority`: *how present is the owner right now?*  It is one
typed, persisted switch with three values -- ``ask-me``, ``critical-only`` and
``away`` -- plus an optional end time.

The rule is **effective permission = authority preset ∩ mode**.  A mode can
only remove a permission the preset already grants; it can never add one.  That
keeps ADR-0001/0005/0008 guarantees intact and stops "away" from becoming a
second, weaker authority system.

Every decision point (dispatch, autonomous start, retry, repair, escalation,
acceptance and candidate publication) asks :func:`decide_owner_action` rather
than inspecting a boolean on its own.  A refusal is a typed
:class:`ModeDecision` with a human-readable reason, so a page or CLI can explain
it without guessing.

Only the owner changes the mode: ``orch mode set|clear`` and the local web
toggle.  MCP exposes a read-only ``get_mode`` and a ``propose_mode_change``
that raises an owner question; it never flips the mode.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any, Final

from saberops.authority import (
    ADVISORY_POLICY_FIELDS,
    ENFORCED_POLICY_FIELDS,
    AuthorityMode,
    AuthorityPolicy,
    AuthoritySettings,
    load_authority_settings,
)
from saberops.authority import (
    label_for as authority_label_for,
)
from saberops.paths import get_owner_mode_path

if TYPE_CHECKING:
    from saberops.questions import OwnerQuestion

__all__ = [
    "ENFORCED_ACTIONS",
    "ModeAction",
    "ModeDecision",
    "OWNER_MODE_LABELS",
    "OWNER_MODE_ORDER",
    "OwnerMode",
    "OwnerModeError",
    "OwnerModeSettings",
    "PACKAGE_DEFAULT_OWNER_MODE",
    "QuestionAnswerDecision",
    "QuestionDisposition",
    "clear_owner_mode",
    "decide_owner_action",
    "decide_question_answer",
    "effective_authority_policy",
    "effective_owner_mode",
    "list_owner_modes",
    "load_effective_owner_mode",
    "load_owner_mode_settings",
    "mode_view",
    "normalize_until",
    "parse_owner_mode",
    "set_owner_mode",
]

SCHEMA_VERSION: Final[int] = 1


class OwnerMode(StrEnum):
    """The three owner-presence modes, narrowest first."""

    ASK_ME = "ask-me"
    CRITICAL_ONLY = "critical-only"
    AWAY = "away"


#: Stable product order; CLI choices and the web selector render exactly this.
OWNER_MODE_ORDER: Final[tuple[OwnerMode, ...]] = (
    OwnerMode.ASK_ME,
    OwnerMode.CRITICAL_ONLY,
    OwnerMode.AWAY,
)

OWNER_MODE_LABELS: Final[dict[OwnerMode, str]] = {
    OwnerMode.ASK_ME: "Ask me",
    OwnerMode.CRITICAL_ONLY: "Critical only",
    OwnerMode.AWAY: "Away",
}

#: A fresh install asks the owner; absence of a saved mode is never "away".
PACKAGE_DEFAULT_OWNER_MODE: Final[OwnerMode] = OwnerMode.ASK_ME


class OwnerModeError(ValueError):
    """Fail-closed error for an unknown mode or a malformed mode document."""


class ModeAction(StrEnum):
    """One permission decision the engine can ask the owner mode about."""

    #: Start a job the owner (or the planning session) submitted.
    DISPATCH = "dispatch"
    #: An autonomous supervisor starts the first attempt of its run.
    AUTONOMOUS_START = "autonomous_start"
    RETRY = "retry"
    REPAIR = "repair"
    ESCALATION = "escalation"
    ACCEPT = "accept"
    PUBLISH = "publish"
    PUBLISH_CANDIDATE = "publish_candidate"
    SAFE_CLEANUP = "safe_cleanup"


_ACTION_POLICY_FIELD: Final[dict[ModeAction, str]] = {
    ModeAction.AUTONOMOUS_START: "auto_start",
    ModeAction.RETRY: "auto_retry",
    ModeAction.REPAIR: "auto_repair",
    ModeAction.ESCALATION: "auto_escalate_when_policy_justifies",
    ModeAction.ACCEPT: "auto_accept",
    ModeAction.PUBLISH: "auto_publish",
    ModeAction.PUBLISH_CANDIDATE: "auto_publish_candidate",
    ModeAction.SAFE_CLEANUP: "auto_safe_cleanup",
}

#: Actions a live code path actually consults the decision for today.  The
#: others (accept, publish, safe cleanup) are recorded intent only and must be
#: labelled advisory until their own lanes wire them.
ENFORCED_ACTIONS: Final[frozenset[ModeAction]] = frozenset(
    {
        ModeAction.DISPATCH,
        ModeAction.AUTONOMOUS_START,
        ModeAction.RETRY,
        ModeAction.REPAIR,
        ModeAction.ESCALATION,
        ModeAction.PUBLISH_CANDIDATE,
    }
)


@dataclass(frozen=True)
class OwnerModeSettings:
    """The persisted owner-mode selection and where it came from."""

    mode: OwnerMode
    until: str | None
    set_at: str | None
    set_by: str | None
    source: str  # "owner" when a config file was read, else "package_default"
    source_path: str | None

    @property
    def label(self) -> str:
        """Stable user-facing label for the declared mode."""
        return OWNER_MODE_LABELS[self.mode]

    def as_dict(self) -> dict[str, Any]:
        """Serialize the declared selection (not the effective intersection)."""
        return {
            "version": SCHEMA_VERSION,
            "mode": self.mode.value,
            "label": self.label,
            "until": self.until,
            "set_at": self.set_at,
            "set_by": self.set_by,
            "source": self.source,
            "source_path": self.source_path,
            "config_path": str(get_owner_mode_path()),
            "package_default_mode": PACKAGE_DEFAULT_OWNER_MODE.value,
        }


@dataclass(frozen=True)
class ModeDecision:
    """One typed answer to "may the engine do this without the owner?"."""

    action: ModeAction
    allowed: bool
    mode: OwnerMode
    declared_mode: OwnerMode
    authority_mode: AuthorityMode
    reason: str
    until: str | None
    expired: bool
    effective_policy: AuthorityPolicy

    def as_dict(self) -> dict[str, Any]:
        """Serialize the decision for CLI/MCP output and audit records."""
        return {
            "action": self.action.value,
            "allowed": self.allowed,
            "enforced": self.action in ENFORCED_ACTIONS,
            "mode": self.mode.value,
            "declared_mode": self.declared_mode.value,
            "authority_mode": self.authority_mode.value,
            "reason": self.reason,
            "until": self.until,
            "expired": self.expired,
            "effective_policy": self.effective_policy.as_dict(),
        }


def parse_owner_mode(value: object) -> OwnerMode:
    """Resolve an exact mode id, failing closed on anything unrecognized."""
    if not isinstance(value, str):
        raise OwnerModeError(f"owner mode must be a string, got {type(value).__name__}")
    for mode in OWNER_MODE_ORDER:
        if value == mode.value:
            return mode
    known = ", ".join(mode.value for mode in OWNER_MODE_ORDER)
    raise OwnerModeError(f"Unknown owner mode {value!r}; known modes: {known}")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_until(value: object) -> str | None:
    """Parse and normalize an optional ISO-8601 end time (timezone required)."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise OwnerModeError("until must be an ISO-8601 string")
    cleaned = value.strip()
    try:
        parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OwnerModeError(
            f"until must be an ISO-8601 timestamp with a timezone, got {cleaned!r}"
        ) from exc
    if parsed.tzinfo is None:
        raise OwnerModeError("until must include a timezone offset or Z")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def normalize_until(value: object) -> str | None:
    """Public form of :func:`_parse_until` for a proposal or a setter."""
    return _parse_until(value)


def _validate_document(
    data: object, source_label: str
) -> tuple[OwnerMode, str | None, str | None, str | None]:
    if not isinstance(data, dict):
        raise OwnerModeError(
            f"Owner mode config at {source_label}: top-level must be a JSON object"
        )
    allowed_keys = {"version", "mode", "until", "set_at", "set_by"}
    unknown = set(data.keys()) - allowed_keys
    if unknown:
        raise OwnerModeError(
            f"Owner mode config at {source_label}: unknown keys {sorted(unknown)}"
        )
    if "version" not in data:
        raise OwnerModeError(f"Owner mode config at {source_label}: missing 'version'")
    version = data["version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise OwnerModeError(
            f"Owner mode config at {source_label}: version must be integer "
            f"{SCHEMA_VERSION}, got {version!r}"
        )
    if "mode" not in data:
        raise OwnerModeError(f"Owner mode config at {source_label}: missing 'mode'")
    try:
        mode = parse_owner_mode(data["mode"])
    except OwnerModeError as exc:
        raise OwnerModeError(f"Owner mode config at {source_label}: {exc}") from exc
    until = _parse_until(data.get("until"))
    if until is not None and mode is not OwnerMode.AWAY:
        raise OwnerModeError(
            f"Owner mode config at {source_label}: 'until' is only valid with away"
        )
    set_at = data.get("set_at")
    set_by = data.get("set_by")
    if set_at is not None and not isinstance(set_at, str):
        raise OwnerModeError("Owner mode config: set_at must be a string")
    if set_by is not None and not isinstance(set_by, str):
        raise OwnerModeError("Owner mode config: set_by must be a string")
    return mode, until, set_at, set_by


def load_owner_mode_settings() -> OwnerModeSettings:
    """Load the persisted owner mode, failing closed on corruption.

    A missing document is not an error: the safest mode (``ask-me``) applies.
    A document that exists but cannot be read, parsed or validated raises rather
    than silently falling back to any other mode.
    """
    path = get_owner_mode_path()
    if not path.is_file():
        return OwnerModeSettings(
            mode=PACKAGE_DEFAULT_OWNER_MODE,
            until=None,
            set_at=None,
            set_by=None,
            source="package_default",
            source_path=None,
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OwnerModeError(f"Owner mode config at {path}: cannot read: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OwnerModeError(f"Owner mode config at {path}: invalid JSON: {exc}") from exc
    mode, until, set_at, set_by = _validate_document(data, str(path))
    return OwnerModeSettings(
        mode=mode,
        until=until,
        set_at=set_at,
        set_by=set_by,
        source="owner",
        source_path=str(path),
    )


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".owner-mode-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def set_owner_mode(
    mode: OwnerMode | str,
    *,
    until: str | None = None,
    set_by: str = "owner",
) -> Path:
    """Validate and atomically persist the owner mode selection.

    ``until`` is only valid with ``away``: an end time on an attended mode has
    no defined meaning, so it fails closed instead of being silently ignored.
    """
    resolved = mode if isinstance(mode, OwnerMode) else parse_owner_mode(mode)
    end = _parse_until(until)
    if end is not None and resolved is not OwnerMode.AWAY:
        raise OwnerModeError("until is only valid with away")
    if not isinstance(set_by, str) or not set_by.strip():
        raise OwnerModeError("set_by must be non-empty text")
    path = get_owner_mode_path()
    payload = (
        json.dumps(
            {
                "version": SCHEMA_VERSION,
                "mode": resolved.value,
                "until": end,
                "set_at": _now_iso(),
                "set_by": set_by.strip(),
            },
            indent=2,
        )
        + "\n"
    )
    _atomic_write(path, payload)
    return path


def clear_owner_mode() -> Path | None:
    """Remove the persisted selection; the packaged ``ask-me`` default applies."""
    path = get_owner_mode_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    return path


def effective_owner_mode(
    settings: OwnerModeSettings, *, now: datetime | None = None
) -> tuple[OwnerMode, bool]:
    """Return ``(effective_mode, expired)``.

    An ``away`` mode whose ``until`` has passed narrows to ``ask-me``.  An
    expired grant must never keep acting, and it must never silently become a
    different grant.
    """
    if settings.mode is not OwnerMode.AWAY or settings.until is None:
        return settings.mode, False
    current = now if now is not None else datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    until = _parse_until(settings.until)
    assert until is not None  # validated on load
    end = datetime.fromisoformat(until.replace("Z", "+00:00"))
    if end <= current:
        return OwnerMode.ASK_ME, True
    return OwnerMode.AWAY, False


def load_effective_owner_mode(*, now: datetime | None = None) -> OwnerMode:
    """Return the effective mode for the persisted selection."""
    return effective_owner_mode(load_owner_mode_settings(), now=now)[0]


def effective_authority_policy(
    preset: AuthorityPolicy, mode: OwnerMode
) -> AuthorityPolicy:
    """Intersect one authority preset with one owner mode.

    The intersection only removes permissions.  ``ask-me`` clears every
    autonomous recovery and publication grant; ``critical-only`` and ``away``
    keep the preset unchanged.  ``auto_start``, ``auto_gate``, ``auto_review``
    and ``routine_questions`` are not narrowed here because the mode answers
    *who decides*, while those fields are read by the attended run path.
    """
    if mode is not OwnerMode.ASK_ME:
        return preset
    return replace(
        preset,
        auto_retry=False,
        auto_repair=False,
        auto_escalate_when_policy_justifies=False,
        auto_accept=False,
        auto_safe_cleanup=False,
        auto_publish=False,
        auto_publish_candidate=False,
    )


def decide_owner_action(
    action: ModeAction | str,
    *,
    authority_settings: AuthoritySettings | None = None,
    mode_settings: OwnerModeSettings | None = None,
    now: datetime | None = None,
) -> ModeDecision:
    """Return the typed owner-mode decision for one engine action.

    This is the single function every decision point calls.  It reads the
    persisted authority preset and the persisted owner mode, intersects them,
    and explains the result.  It never writes anything.
    """
    resolved = action if isinstance(action, ModeAction) else ModeAction(action)
    authority = authority_settings or load_authority_settings()
    settings = mode_settings or load_owner_mode_settings()
    mode, expired = effective_owner_mode(settings, now=now)
    policy = effective_authority_policy(authority.policy, mode)
    expiry_note = (
        f"away ended at {settings.until}; ask-me applies. " if expired else ""
    )

    if resolved is ModeAction.DISPATCH:
        allowed = policy.auto_start
        reason = expiry_note + (
            "dispatch follows the authority preset; the owner mode never widens "
            f"it: auto_start={'true' if allowed else 'false'}."
        )
        return ModeDecision(
            action=resolved,
            allowed=allowed,
            mode=mode,
            declared_mode=settings.mode,
            authority_mode=authority.mode,
            reason=reason,
            until=settings.until,
            expired=expired,
            effective_policy=policy,
        )

    field = _ACTION_POLICY_FIELD[resolved]
    if mode is OwnerMode.ASK_ME:
        allowed = False
        reason = expiry_note + (
            f"ask-me leaves {resolved.value} decisions to the owner."
        )
    else:
        allowed = bool(getattr(policy, field))
        reason = expiry_note + (
            f"{mode.value} never exceeds the {authority.mode.value} preset: "
            f"{field}={'true' if allowed else 'false'}."
        )
    return ModeDecision(
        action=resolved,
        allowed=allowed,
        mode=mode,
        declared_mode=settings.mode,
        authority_mode=authority.mode,
        reason=reason,
        until=settings.until,
        expired=expired,
        effective_policy=policy,
    )


def list_owner_modes() -> list[dict[str, str]]:
    """Describe all three owner modes in stable product order."""
    return [
        {"mode": mode.value, "label": OWNER_MODE_LABELS[mode]}
        for mode in OWNER_MODE_ORDER
    ]


class QuestionDisposition(StrEnum):
    """What the effective mode permits for one question."""

    #: Critical or attended: the owner answers; the question notifies and waits.
    ASK_OWNER = "ask_owner"
    #: A live planning session may answer with evidence and an alternative.
    PLANNER_MAY_ANSWER = "planner_may_answer"
    #: Routine, but no live planner: the question waits; no background planner.
    WAIT_FOR_PLANNER = "wait_for_planner"


@dataclass(frozen=True)
class QuestionAnswerDecision:
    """The typed answer to "may the planning session answer this question?"."""

    critical: bool
    disposition: QuestionDisposition
    mode: OwnerMode
    declared_mode: OwnerMode
    planner_present: bool
    until: str | None
    expired: bool
    reason: str

    @property
    def allowed(self) -> bool:
        """True only when the live planning session may record the answer."""
        return self.disposition is QuestionDisposition.PLANNER_MAY_ANSWER

    def as_dict(self) -> dict[str, Any]:
        """Serialize the decision for CLI/MCP output and audit records."""
        return {
            "allowed": self.allowed,
            "critical": self.critical,
            "disposition": self.disposition.value,
            "mode": self.mode.value,
            "declared_mode": self.declared_mode.value,
            "planner_present": self.planner_present,
            "until": self.until,
            "expired": self.expired,
            "reason": self.reason,
        }


def decide_question_answer(
    question: OwnerQuestion,
    *,
    planner_present: bool,
    mode_settings: OwnerModeSettings | None = None,
    now: datetime | None = None,
) -> QuestionAnswerDecision:
    """Decide whether a planning session may self-answer one question.

    Criticality is derived from the question, never supplied by the caller; a
    critical question therefore reaches the owner in every mode.  A routine
    question is answered only in ``critical-only`` or ``away`` while a live
    planning session is present.  With no session, the question waits and the
    engine starts no background planner (the work pause itself belongs to the
    close-the-CLI lane).
    """
    from saberops.questions import is_critical_question

    settings = mode_settings or load_owner_mode_settings()
    mode, expired = effective_owner_mode(settings, now=now)
    expiry_note = f"away ended at {settings.until}; ask-me applies. " if expired else ""
    critical = is_critical_question(question)
    if critical:
        disposition = QuestionDisposition.ASK_OWNER
        reason = expiry_note + (
            "the question is critical: it notifies the owner and waits, and no "
            "planning session may answer it."
        )
    elif mode is OwnerMode.ASK_ME:
        disposition = QuestionDisposition.ASK_OWNER
        reason = expiry_note + "ask-me leaves every question to the owner."
    elif not planner_present:
        disposition = QuestionDisposition.WAIT_FOR_PLANNER
        reason = (
            "no live planning session: no background planner runs, so the routine "
            "question waits and work pauses until a planner returns."
        )
    else:
        disposition = QuestionDisposition.PLANNER_MAY_ANSWER
        reason = (
            f"{mode.value} lets the live planning session answer this routine "
            "question with evidence and a rejected alternative; the answer is logged."
        )
    return QuestionAnswerDecision(
        critical=critical,
        disposition=disposition,
        mode=mode,
        declared_mode=settings.mode,
        planner_present=planner_present,
        until=settings.until,
        expired=expired,
        reason=reason,
    )


def mode_view(
    *,
    authority_settings: AuthoritySettings | None = None,
    mode_settings: OwnerModeSettings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Render the effective mode, its intersection and every decision reason."""
    authority = authority_settings or load_authority_settings()
    settings = mode_settings or load_owner_mode_settings()
    mode, expired = effective_owner_mode(settings, now=now)
    policy = effective_authority_policy(authority.policy, mode)
    decisions = {
        action.value: decide_owner_action(
            action,
            authority_settings=authority,
            mode_settings=settings,
            now=now,
        ).as_dict()
        for action in ModeAction
    }
    return {
        "v": SCHEMA_VERSION,
        "mode": mode.value,
        "declared_mode": settings.mode.value,
        "label": OWNER_MODE_LABELS[mode],
        "declared_label": OWNER_MODE_LABELS[settings.mode],
        "until": settings.until,
        "expired": expired,
        "set_at": settings.set_at,
        "set_by": settings.set_by,
        "source": settings.source,
        "source_path": settings.source_path,
        "config_path": str(get_owner_mode_path()),
        "package_default_mode": PACKAGE_DEFAULT_OWNER_MODE.value,
        "authority_mode": authority.mode.value,
        "authority_label": authority_label_for(authority.mode),
        "enforced_fields": sorted(ENFORCED_POLICY_FIELDS),
        "advisory_fields": sorted(ADVISORY_POLICY_FIELDS),
        "effective_policy": policy.as_dict(),
        "decisions": decisions,
    }
