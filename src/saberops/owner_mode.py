"""One persisted owner mode: what may run without the owner.

The owner mode is one typed, persisted switch with three values -- ``ask-me``,
``critical-only`` and ``away`` -- plus an optional end time and job budget. It
is the single owner setting that decides what the engine may do unattended;
the five authority presets (and the old ``preset ∩ mode`` rule) were folded
into it by ADR-0082.

Each mode carries a fixed permission ceiling (:data:`MODE_CEILINGS`). The
persisted document may *narrow* that ceiling -- a store migrated from the
removed authority presets keeps exactly the permissions it had -- but it can
never widen it. Every decision reads ``effective permission = stored grant ∩
mode ceiling``; nothing can add a permission the mode does not carry.

Every decision point (dispatch, autonomous start, retry, repair, escalation,
acceptance and candidate publication) asks :func:`decide_owner_action` rather
than inspecting a boolean on its own. A refusal is a typed
:class:`ModeDecision` with a human-readable reason, so a page or CLI can
explain it without guessing.

Only the owner changes the mode: ``orch mode set|clear``, the local web toggle, an
owner-typed chat command handled by a CLI's owner-prompt hook, or the terminal
confirmation code for a looser change without that hook (ADR-0090).  MCP
exposes a read-only ``get_mode``, a ``propose_mode_change`` that raises an owner
question, and a ``tighten_mode`` that may only switch to ``ask-me``: a planner
can always make the engine more careful, never less.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import TYPE_CHECKING, Any, Final

from saberops.paths import get_owner_mode_path, saberops_config_dir

if TYPE_CHECKING:
    from saberops.questions import OwnerQuestion

__all__ = [
    "ADVISORY_POLICY_FIELDS",
    "ENFORCED_ACTIONS",
    "ENFORCED_POLICY_FIELDS",
    "HostAccessMode",
    "MODE_CEILINGS",
    "ModeAction",
    "ModeDecision",
    "ModePermissions",
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
    "effective_owner_mode",
    "list_owner_modes",
    "load_effective_owner_mode",
    "load_owner_mode_settings",
    "mode_permissions",
    "mode_view",
    "normalize_until",
    "parse_owner_mode",
    "set_host_access",
    "set_owner_mode",
]

SCHEMA_VERSION: Final[int] = 2
_LEGACY_SCHEMA_VERSION: Final[int] = 1

_LOGGER = logging.getLogger(__name__)


class OwnerMode(StrEnum):
    """The three owner-presence modes, narrowest first."""

    ASK_ME = "ask-me"
    CRITICAL_ONLY = "critical-only"
    AWAY = "away"


class HostAccessMode(StrEnum):
    """Technical host capability, independent from the owner mode."""

    GOVERNED = "governed"
    UNRESTRICTED = "unrestricted"


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


@dataclass(frozen=True)
class ModePermissions:
    """What one mode permits without asking the owner.

    Every dimension is an explicit typed field. Nothing here is inferred from
    prose, and no dimension is derived from another at read time.
    """

    auto_start: bool
    auto_retry: bool
    auto_repair: bool
    auto_escalate_when_policy_justifies: bool
    auto_gate: bool
    auto_review: bool
    auto_accept: bool
    auto_safe_cleanup: bool
    auto_publish: bool
    #: Publication of a verified *candidate branch* to the remote for external
    #: review, distinct from ``auto_publish`` (never granted by a mode; a run
    #: may still opt in explicitly with ``--publish-candidate``).
    auto_publish_candidate: bool
    routine_questions: bool

    def as_dict(self) -> dict[str, bool]:
        """Return the permissions as a plain JSON-compatible mapping."""
        return {key: bool(value) for key, value in asdict(self).items()}


_PERMISSION_FIELDS: Final[tuple[str, ...]] = tuple(ModePermissions.__dataclass_fields__)


def _permissions(bits: str) -> ModePermissions:
    """Build one row from a bit string in :data:`_PERMISSION_FIELDS` order."""
    return ModePermissions(
        **{
            field: bit == "1"
            for field, bit in zip(_PERMISSION_FIELDS, bits, strict=True)
        }
    )


#: What each mode is allowed to permit.  A persisted document can only narrow
#: this table (see :func:`mode_permissions`); nothing can widen it.
#:
#: ``ask-me`` keeps the attended start/gate/review path and nothing automated
#: beyond it.  ``critical-only`` and ``away`` converge and accept a gated,
#: independently reviewed candidate as a local commit (ADR-0008); neither ever
#: publishes, and candidate publication stays an explicit per-run choice.
_ATTENDED_CEILING: Final[ModePermissions] = _permissions("10001100001")
_UNATTENDED_CEILING: Final[ModePermissions] = _permissions("11111111001")
MODE_CEILINGS: Final[dict[OwnerMode, ModePermissions]] = {
    OwnerMode.ASK_ME: _ATTENDED_CEILING,
    OwnerMode.CRITICAL_ONLY: _UNATTENDED_CEILING,
    OwnerMode.AWAY: _UNATTENDED_CEILING,
}

#: Policy dimensions a live decision path reads today.  Everything else in
#: :class:`ModePermissions` is a recorded intent only: no engine code consults
#: it, so the CLI and web surfaces must label it advisory rather than promise
#: it as a live permission.
ENFORCED_POLICY_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "auto_start",
        "auto_retry",
        "auto_repair",
        "auto_escalate_when_policy_justifies",
        "auto_accept",
        "auto_publish_candidate",
    }
)

#: Every policy dimension that is *not* read by live code yet.
ADVISORY_POLICY_FIELDS: Final[frozenset[str]] = (
    frozenset(_PERMISSION_FIELDS) - ENFORCED_POLICY_FIELDS
)


# --------------------------------------------------------------------------- #
# The frozen legacy authority presets (migration input only)
# --------------------------------------------------------------------------- #

#: The five removed authority presets, frozen exactly as they were on local
#: ``main`` before ADR-0082.  This table is read *only* to migrate a store
#: written before the fold; it never decides a live permission afterwards.
#: Fields: start, retry, repair, escalate, gate, review, accept, cleanup,
#: publish, candidate publication, routine questions.
_LEGACY_PRESET_PERMISSIONS: Final[dict[str, ModePermissions]] = {
    "ask_before_actions": _permissions("00000000001"),
    "supervised": _permissions("10001100001"),
    "autonomous": _permissions("11111101010"),
    "full_autonomy": _permissions("11111111010"),
    "full_autonomy_publish": _permissions("11111111110"),
}

#: The pre-1.1 packaged default preset (the authority document was optional).
_LEGACY_PACKAGE_DEFAULT_PRESET: Final[str] = "supervised"

_MIGRATION_NOTICE_TEMPLATE: Final[str] = (
    "Owner settings migrated to one mode document (ADR-0082): the authority "
    "preset '{preset}' is folded into the owner mode and the saved permissions "
    "are preserved; nothing widened. Use `orch mode show` to review them."
)


def _legacy_effective_permissions(preset: str, mode: OwnerMode) -> ModePermissions:
    """Return the pre-1.1 effective permissions for one preset x mode pair.

    This is the frozen old reader: ``ask-me`` cleared every autonomous recovery,
    acceptance, cleanup and publication grant; the other modes kept the preset.
    """
    base = _LEGACY_PRESET_PERMISSIONS[preset]
    if mode is not OwnerMode.ASK_ME:
        return base
    return replace(
        base,
        auto_retry=False,
        auto_repair=False,
        auto_escalate_when_policy_justifies=False,
        auto_accept=False,
        auto_safe_cleanup=False,
        auto_publish=False,
        auto_publish_candidate=False,
    )


def _narrow(base: ModePermissions, ceiling: ModePermissions) -> ModePermissions:
    """Return the intersection of a stored grant and a mode ceiling."""
    return replace(
        base,
        auto_start=base.auto_start and ceiling.auto_start,
        auto_retry=base.auto_retry and ceiling.auto_retry,
        auto_repair=base.auto_repair and ceiling.auto_repair,
        auto_escalate_when_policy_justifies=(
            base.auto_escalate_when_policy_justifies
            and ceiling.auto_escalate_when_policy_justifies
        ),
        auto_gate=base.auto_gate and ceiling.auto_gate,
        auto_review=base.auto_review and ceiling.auto_review,
        auto_accept=base.auto_accept and ceiling.auto_accept,
        auto_safe_cleanup=base.auto_safe_cleanup and ceiling.auto_safe_cleanup,
        auto_publish=base.auto_publish and ceiling.auto_publish,
        auto_publish_candidate=(
            base.auto_publish_candidate and ceiling.auto_publish_candidate
        ),
        routine_questions=base.routine_questions and ceiling.routine_questions,
    )


def mode_permissions(
    mode: OwnerMode, grant: ModePermissions | None = None
) -> ModePermissions:
    """Return the effective permissions for a mode and an optional stored grant.

    The ceiling is the product table; the stored grant (a migrated store, or a
    document narrowed by hand) can only remove permissions from it.
    """
    ceiling = MODE_CEILINGS[mode]
    return ceiling if grant is None else _narrow(grant, ceiling)


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
#: others (publish, safe cleanup) are recorded intent only and must be
#: labelled advisory until their own lanes wire them.  Acceptance is enforced
#: by the F15 auto-accept pass (:mod:`saberops.auto_accept`).
ENFORCED_ACTIONS: Final[frozenset[ModeAction]] = frozenset(
    {
        ModeAction.DISPATCH,
        ModeAction.AUTONOMOUS_START,
        ModeAction.RETRY,
        ModeAction.REPAIR,
        ModeAction.ESCALATION,
        ModeAction.ACCEPT,
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
    #: F17: away-mode job budget for this session; ``None`` means "no bound".
    max_jobs: int | None = None
    #: Technical host capability, kept here because the authority document
    #: that used to hold it is gone (ADR-0082).
    host_access: HostAccessMode = HostAccessMode.GOVERNED
    #: A stored grant narrower than the mode ceiling (a migrated store).
    #: ``None`` means the mode's full ceiling.
    grant: ModePermissions | None = None
    #: One human-readable notice when this read migrated an older store.
    notice: str | None = None

    @property
    def away_bounded(self) -> bool:
        """True when an away session carries at least one end bound.

        The owner decision (2026-11-19) is that away never runs unbounded: it
        needs an end time, a job count, or both.  A document written before
        F17 therefore loads as *unbounded* and the runtime gate refuses to
        start work rather than silently treating it as bounded.
        """
        return self.until is not None or self.max_jobs is not None

    @property
    def label(self) -> str:
        """Stable user-facing label for the declared mode."""
        return OWNER_MODE_LABELS[self.mode]

    @property
    def permissions(self) -> ModePermissions:
        """Effective permissions for the declared mode (expiry not applied)."""
        return mode_permissions(self.mode, self.grant)



@dataclass(frozen=True)
class ModeDecision:
    """One typed answer to "may the engine do this without the owner?"."""

    action: ModeAction
    allowed: bool
    mode: OwnerMode
    declared_mode: OwnerMode
    reason: str
    until: str | None
    expired: bool
    effective_policy: ModePermissions

    def as_dict(self) -> dict[str, Any]:
        """Serialize the decision for CLI/MCP output and audit records."""
        return {
            "action": self.action.value,
            "allowed": self.allowed,
            "enforced": self.action in ENFORCED_ACTIONS,
            "mode": self.mode.value,
            "declared_mode": self.declared_mode.value,
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


def _parse_host_access(value: object) -> HostAccessMode:
    if not isinstance(value, str):
        raise OwnerModeError("host_access must be a string")
    try:
        return HostAccessMode(value)
    except ValueError as exc:
        raise OwnerModeError(f"Unknown host_access mode {value!r}") from exc


def _parse_permissions(data: object, source_label: str) -> ModePermissions:
    if not isinstance(data, dict):
        raise OwnerModeError(
            f"Owner mode config at {source_label}: permissions must be a JSON object"
        )
    unknown = set(data.keys()) - set(_PERMISSION_FIELDS)
    if unknown:
        raise OwnerModeError(
            f"Owner mode config at {source_label}: unknown permission fields "
            f"{sorted(unknown)}"
        )
    missing = set(_PERMISSION_FIELDS) - set(data.keys())
    if missing:
        raise OwnerModeError(
            f"Owner mode config at {source_label}: missing permission fields "
            f"{sorted(missing)}"
        )
    values: dict[str, bool] = {}
    for name in _PERMISSION_FIELDS:
        value = data[name]
        if not isinstance(value, bool):
            raise OwnerModeError(
                f"Owner mode config at {source_label}: permission {name!r} must "
                "be a boolean"
            )
        values[name] = value
    return ModePermissions(**values)


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


def _parse_max_jobs(value: object) -> int | None:
    """Parse an optional away-mode job budget (positive integer or None)."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise OwnerModeError("max_jobs must be a positive integer")
    if value < 1:
        raise OwnerModeError("max_jobs must be a positive integer")
    return value


def _validate_mode_fields(
    data: dict[str, Any], source_label: str
) -> tuple[OwnerMode, str | None, str | None, str | None, int | None]:
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
    max_jobs = _parse_max_jobs(data.get("max_jobs"))
    if max_jobs is not None and mode is not OwnerMode.AWAY:
        raise OwnerModeError(
            f"Owner mode config at {source_label}: 'max_jobs' is only valid with away"
        )
    set_at = data.get("set_at")
    set_by = data.get("set_by")
    if set_at is not None and not isinstance(set_at, str):
        raise OwnerModeError("Owner mode config: set_at must be a string")
    if set_by is not None and not isinstance(set_by, str):
        raise OwnerModeError("Owner mode config: set_by must be a string")
    return mode, until, set_at, set_by, max_jobs


def _validate_document(
    data: object, source_label: str, *, legacy: bool = False
) -> tuple[
    OwnerMode,
    str | None,
    str | None,
    str | None,
    int | None,
    HostAccessMode,
    ModePermissions | None,
]:
    """Validate one mode document, failing closed on anything unexpected."""
    if not isinstance(data, dict):
        raise OwnerModeError(
            f"Owner mode config at {source_label}: top-level must be a JSON object"
        )
    allowed_keys = {"version", "mode", "until", "set_at", "set_by", "max_jobs"}
    if not legacy:
        allowed_keys |= {"host_access", "permissions"}
    unknown = set(data.keys()) - allowed_keys
    if unknown:
        raise OwnerModeError(
            f"Owner mode config at {source_label}: unknown keys {sorted(unknown)}"
        )
    if "version" not in data:
        raise OwnerModeError(f"Owner mode config at {source_label}: missing 'version'")
    expected = _LEGACY_SCHEMA_VERSION if legacy else SCHEMA_VERSION
    version = data["version"]
    if type(version) is not int or version != expected:
        raise OwnerModeError(
            f"Owner mode config at {source_label}: version must be integer "
            f"{expected}, got {version!r}"
        )
    mode, until, set_at, set_by, max_jobs = _validate_mode_fields(data, source_label)
    host_access = _parse_host_access(
        data.get("host_access", HostAccessMode.GOVERNED.value)
    )
    permissions = (
        None
        if data.get("permissions") is None
        else _parse_permissions(data["permissions"], source_label)
    )
    return mode, until, set_at, set_by, max_jobs, host_access, permissions


def _legacy_authority_path() -> Path:
    """Return the removed authority document path (migration input only)."""
    return saberops_config_dir() / "authority.json"


def _read_legacy_authority() -> tuple[str, HostAccessMode] | None:
    """Read a pre-1.1 ``authority.json``, failing closed on corruption."""
    path = _legacy_authority_path()
    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OwnerModeError(f"Authority config at {path}: cannot read: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OwnerModeError(f"Authority config at {path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise OwnerModeError(
            f"Authority config at {path}: top-level must be a JSON object"
        )
    unknown = set(data.keys()) - {"version", "mode", "host_access"}
    if unknown:
        raise OwnerModeError(f"Authority config at {path}: unknown keys {sorted(unknown)}")
    if data.get("version") != _LEGACY_SCHEMA_VERSION or type(
        data.get("version")
    ) is not int:
        raise OwnerModeError(
            f"Authority config at {path}: version must be integer "
            f"{_LEGACY_SCHEMA_VERSION}, got {data.get('version')!r}"
        )
    preset = data.get("mode")
    if not isinstance(preset, str) or preset not in _LEGACY_PRESET_PERMISSIONS:
        known = ", ".join(sorted(_LEGACY_PRESET_PERMISSIONS))
        raise OwnerModeError(
            f"Authority config at {path}: unknown authority mode {preset!r}; "
            f"known modes: {known}"
        )
    host_access = _parse_host_access(
        data.get("host_access", HostAccessMode.GOVERNED.value)
    )
    return preset, host_access


def _migration_notice(preset: str) -> str:
    return _MIGRATION_NOTICE_TEMPLATE.format(preset=preset)


def _write_document(settings: OwnerModeSettings, *, path: Path | None = None) -> Path:
    """Atomically persist one v2 owner-mode document."""
    target = path if path is not None else get_owner_mode_path()
    payload = (
        json.dumps(
            {
                "version": SCHEMA_VERSION,
                "mode": settings.mode.value,
                "until": settings.until,
                "set_at": settings.set_at,
                "set_by": settings.set_by,
                "max_jobs": settings.max_jobs,
                "host_access": settings.host_access.value,
                "permissions": (settings.grant or settings.permissions).as_dict(),
            },
            indent=2,
        )
        + "\n"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=".owner-mode-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
    return target


def _migrate(settings: OwnerModeSettings, *, preset: str, path: Path | None = None) -> None:
    """Write the v2 document once and retire the removed authority document."""
    try:
        _write_document(settings, path=path)
    except OSError as exc:  # a read-only store keeps working, one notice at a time
        _LOGGER.warning("owner settings migration write failed: %s", exc)
        return
    legacy = _legacy_authority_path()
    if legacy.is_file():
        try:
            legacy.replace(legacy.with_name(f"{legacy.name}.migrated"))
        except OSError as exc:  # pragma: no cover - the v2 document already wins
            _LOGGER.warning("legacy authority document rename failed: %s", exc)
    _LOGGER.info(_migration_notice(preset))


def load_owner_mode_settings() -> OwnerModeSettings:
    """Load the persisted owner mode, failing closed on corruption.

    A missing document is not an error: the safest mode (``ask-me``) applies.
    A pre-1.1 document (the removed authority preset, alone or beside a v1 mode
    document) is migrated once, with one notice, and never widens a permission.
    """
    path = get_owner_mode_path()
    if not path.is_file():
        legacy = _read_legacy_authority()
        if legacy is None:
            return OwnerModeSettings(
                mode=PACKAGE_DEFAULT_OWNER_MODE,
                until=None,
                set_at=None,
                set_by=None,
                source="package_default",
                source_path=None,
            )
        preset, host_access = legacy
        migrated = OwnerModeSettings(
            mode=PACKAGE_DEFAULT_OWNER_MODE,
            until=None,
            set_at=None,
            set_by=None,
            source="owner",
            source_path=str(path),
            host_access=host_access,
            grant=_legacy_effective_permissions(preset, PACKAGE_DEFAULT_OWNER_MODE),
            notice=_migration_notice(preset),
        )
        _migrate(migrated, preset=preset)
        return migrated
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise OwnerModeError(f"Owner mode config at {path}: cannot read: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise OwnerModeError(f"Owner mode config at {path}: invalid JSON: {exc}") from exc
    if isinstance(data, dict) and data.get("version") == SCHEMA_VERSION:
        (
            mode,
            until,
            set_at,
            set_by,
            max_jobs,
            host_access,
            permissions,
        ) = _validate_document(data, str(path))
        return OwnerModeSettings(
            mode=mode,
            until=until,
            set_at=set_at,
            set_by=set_by,
            source="owner",
            source_path=str(path),
            max_jobs=max_jobs,
            host_access=host_access,
            grant=permissions,
        )
    # A v1 mode document (or any older/unversioned one): validate it as legacy,
    # then migrate it together with the removed authority document, if present.
    mode, until, set_at, set_by, max_jobs, _host, _perms = _validate_document(
        data, str(path), legacy=True
    )
    legacy = _read_legacy_authority()
    preset, host_access = (
        legacy
        if legacy is not None
        else (_LEGACY_PACKAGE_DEFAULT_PRESET, HostAccessMode.GOVERNED)
    )
    migrated = OwnerModeSettings(
        mode=mode,
        until=until,
        set_at=set_at,
        set_by=set_by,
        source="owner",
        source_path=str(path),
        max_jobs=max_jobs,
        host_access=host_access,
        grant=_legacy_effective_permissions(preset, mode),
        notice=_migration_notice(preset),
    )
    _migrate(migrated, preset=preset, path=path)
    return migrated


def set_owner_mode(
    mode: OwnerMode | str,
    *,
    until: str | None = None,
    max_jobs: int | None = None,
    set_by: str = "owner",
) -> Path:
    """Validate and atomically persist the owner mode selection.

    ``until`` and ``max_jobs`` are only valid with ``away``: an end time or job
    budget on an attended mode has no defined meaning, so it fails closed
    instead of being silently ignored.  An ``away`` selection must carry at
    least one bound (F17); the owner decision is that away never runs without a
    way to stop it.

    An explicit selection applies that mode's ceiling.  A store migrated from
    the removed presets keeps its narrower grant until the owner switches mode
    on purpose (ADR-0082).
    """
    resolved = mode if isinstance(mode, OwnerMode) else parse_owner_mode(mode)
    end = _parse_until(until)
    budget = _parse_max_jobs(max_jobs)
    if end is not None and resolved is not OwnerMode.AWAY:
        raise OwnerModeError("until is only valid with away")
    if budget is not None and resolved is not OwnerMode.AWAY:
        raise OwnerModeError("max_jobs is only valid with away")
    if resolved is OwnerMode.AWAY and end is None and budget is None:
        raise OwnerModeError(
            "away needs an end time or a job budget (--until/--max-jobs); "
            "an unbounded away session is refused"
        )
    if not isinstance(set_by, str) or not set_by.strip():
        raise OwnerModeError("set_by must be non-empty text")
    current = load_owner_mode_settings()
    settings = OwnerModeSettings(
        mode=resolved,
        until=end,
        set_at=_now_iso(),
        set_by=set_by.strip(),
        source="owner",
        source_path=str(get_owner_mode_path()),
        max_jobs=budget,
        host_access=current.host_access,
        grant=MODE_CEILINGS[resolved],
    )
    return _write_document(settings)


def clear_owner_mode() -> Path | None:
    """Remove the persisted selection; the packaged ``ask-me`` default applies."""
    path = get_owner_mode_path()
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    return path


def set_host_access(mode: HostAccessMode | str) -> Path:
    """Persist the technical host access inside the one owner document."""
    resolved = mode if isinstance(mode, HostAccessMode) else _parse_host_access(mode)
    current = load_owner_mode_settings()
    settings = OwnerModeSettings(
        mode=current.mode,
        until=current.until,
        set_at=current.set_at or _now_iso(),
        set_by=current.set_by or "owner",
        source="owner",
        source_path=str(get_owner_mode_path()),
        max_jobs=current.max_jobs,
        host_access=resolved,
        grant=current.grant,
    )
    return _write_document(settings)


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


def decide_owner_action(
    action: ModeAction | str,
    *,
    mode_settings: OwnerModeSettings | None = None,
    now: datetime | None = None,
) -> ModeDecision:
    """Return the typed owner-mode decision for one engine action.

    This is the single function every decision point calls.  It reads the
    persisted owner document, applies the effective mode's ceiling to the
    stored grant, and explains the result.  It never writes anything.
    """
    resolved = action if isinstance(action, ModeAction) else ModeAction(action)
    settings = mode_settings or load_owner_mode_settings()
    mode, expired = effective_owner_mode(settings, now=now)
    policy = mode_permissions(mode, settings.grant)
    expiry_note = f"away ended at {settings.until}; ask-me applies. " if expired else ""

    if resolved is ModeAction.DISPATCH:
        allowed = policy.auto_start
        reason = expiry_note + (
            "dispatch follows the owner mode's auto_start; the mode never widens "
            f"it: auto_start={'true' if allowed else 'false'}."
        )
        return ModeDecision(
            action=resolved,
            allowed=allowed,
            mode=mode,
            declared_mode=settings.mode,
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
            f"{mode.value} permits {resolved.value} "
            f"({field}={'true' if allowed else 'false'})."
        )
    return ModeDecision(
        action=resolved,
        allowed=allowed,
        mode=mode,
        declared_mode=settings.mode,
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
    mode_settings: OwnerModeSettings | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Render the effective mode, its permissions and every decision reason."""
    settings = mode_settings or load_owner_mode_settings()
    mode, expired = effective_owner_mode(settings, now=now)
    policy = mode_permissions(mode, settings.grant)
    decisions = {
        action.value: decide_owner_action(
            action,
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
        "max_jobs": settings.max_jobs,
        "away_bounded": settings.away_bounded,
        "source": settings.source,
        "source_path": settings.source_path,
        "config_path": str(get_owner_mode_path()),
        "package_default_mode": PACKAGE_DEFAULT_OWNER_MODE.value,
        "host_access": settings.host_access.value,
        "notice": settings.notice,
        "enforced_fields": sorted(ENFORCED_POLICY_FIELDS),
        "advisory_fields": sorted(ADVISORY_POLICY_FIELDS),
        "effective_policy": policy.as_dict(),
        "decisions": decisions,
    }
