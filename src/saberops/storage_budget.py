"""Deterministic host-wide storage budget and reservations for concurrent work.

Covers: ADR-0003 Amendment 2 (concurrent-run adaptive storage admission).

The Orchestrator *estimates*; this module *decides and enforces*.  Nothing here
calls a model, a provider or the network: every number is either a declared
bound supplied by the caller, a live ``statvfs`` measurement, or a value read
back from the durable ledger.

Design in one paragraph.  One owner-only SQLite ledger, guarded by one
host-wide ``flock`` critical section, is authoritative for every project,
repository and CLI session of one user on one host.  Inside that critical
section the module measures the live free space of each filesystem the
candidate touches, sums the *remaining* growth bound of every admitted live
reservation on the same filesystem, adds the candidate's own conservative
bound and a calibrated recovery margin, and admits only when the measurement
covers the sum.  Bytes already on disk are already reflected in the
measurement, so reservations track **future growth only**.  Unknown inputs, an
unapproved margin policy, a corrupt or foreign ledger, lock contention and
unreadable filesystems all fail closed with a typed ``DISK_BUDGET_*`` result
that reserves nothing.

Deliberate scope limits (see ADR-0003 Amendment 2 §Rollout): this slice is not
wired into ``scripts/gate_tools.py`` or the engine capacity loop.  The existing
live free-space floor therefore remains binding until the later, separately
reviewed integration replaces every launch path.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import socket
import sqlite3
import stat as stat_module
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, TypeVar

from saberops import paths as saberops_paths
from saberops.ownership import Liveness, ProcessIdentity, probe_liveness

__all__ = [
    "DEFAULT_BUSY_TIMEOUT_SECONDS",
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_LOCK_TIMEOUT_SECONDS",
    "ESTIMATE_VERSION",
    "LEDGER_DIRNAME",
    "LEDGER_FILENAME",
    "LOCK_FILENAME",
    "MAX_RENEWAL_FACTOR",
    "MODEL_CALLS",
    "POLICY_FILENAME",
    "REASON_EXCEEDED",
    "REASON_OK",
    "REASON_UNAVAILABLE",
    "SCHEMA_VERSION",
    "SHADOW_MARGIN_BYTES",
    "AdmissionOutcome",
    "Attribution",
    "AttributionUnavailableError",
    "Contribution",
    "EstimateComponent",
    "EstimateSource",
    "EstimateUnavailableError",
    "FencingError",
    "FilesystemIdentity",
    "FilesystemIdentityError",
    "FilesystemProbe",
    "FsBudget",
    "FsSample",
    "LedgerConfig",
    "LedgerCorruptError",
    "LedgerSecurityError",
    "LedgerUnavailableError",
    "LockContentionError",
    "MarginPolicy",
    "PolicyUnavailableError",
    "RenewalBoundError",
    "Reservation",
    "ReservationEvent",
    "ReservationKey",
    "ReservationState",
    "StorageBudgetError",
    "StorageDecision",
    "StorageEstimate",
    "StorageLedger",
    "canonical_ledger_root",
    "compute_required_free",
    "default_policy_path",
    "load_margin_policy",
    "real_filesystem_identity",
]

#: Ledger schema version.  A mismatch is corruption, never a silent upgrade.
SCHEMA_VERSION: Final[int] = 1
#: Caller-declared estimate contract version.
ESTIMATE_VERSION: Final[int] = 1
#: No model call happens on any path in this module.
MODEL_CALLS: Final[int] = 0

LEDGER_DIRNAME: Final[str] = "storage-budget"
LEDGER_FILENAME: Final[str] = "reservations.sqlite3"
LOCK_FILENAME: Final[str] = "reservations.lock"
POLICY_FILENAME: Final[str] = "storage_budget.json"

DEFAULT_LOCK_TIMEOUT_SECONDS: Final[float] = 15.0
DEFAULT_BUSY_TIMEOUT_SECONDS: Final[float] = 15.0
DEFAULT_LEASE_SECONDS: Final[float] = 900.0

#: A single renewal may at most double a reservation's declared bound.  Beyond
#: that the caller must publish a new estimate version instead of growing one
#: reservation without limit.
MAX_RENEWAL_FACTOR: Final[float] = 2.0

#: Shadow-only placeholder recovery margin.  It is used *exclusively* by
#: shadow reports (``margin_authorised=False``) and can never authorise an
#: admission: ADR-0003 Amendment 2 requires an owner-approved numeric policy
#: before the margin becomes admission fact.  Deliberately below the old
#: fixed ``max(10 GiB, 10 %)`` floor, never zero.
SHADOW_MARGIN_BYTES: Final[int] = 1 << 30

REASON_OK: Final[str] = "OK"
REASON_EXCEEDED: Final[str] = "DISK_BUDGET_EXCEEDED"
REASON_UNAVAILABLE: Final[str] = "DISK_BUDGET_UNAVAILABLE"

_KEY_PART_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


# --------------------------------------------------------------------------
# Typed, fail-closed errors
# --------------------------------------------------------------------------


class StorageBudgetError(RuntimeError):
    """Base class: every failure maps to a typed, fail-closed reason code."""

    reason_code: str = REASON_UNAVAILABLE


class LockContentionError(StorageBudgetError):
    """The host-wide admission lock was not acquired inside its bound."""

    reason_code = "LOCK_CONTENTION"


class LedgerUnavailableError(StorageBudgetError):
    """The ledger could not be opened, read or written."""

    reason_code = "LEDGER_UNAVAILABLE"


class LedgerCorruptError(StorageBudgetError):
    """The ledger exists but is incomplete, unreadable or a foreign schema."""

    reason_code = "LEDGER_CORRUPT"


class LedgerSecurityError(StorageBudgetError):
    """The ledger path/permissions would redirect or duplicate owner state."""

    reason_code = "LEDGER_INSECURE"


class PolicyUnavailableError(StorageBudgetError):
    """No valid owner-approved recovery-margin policy is recorded."""

    reason_code = "MARGIN_POLICY_UNAVAILABLE"


class EstimateUnavailableError(StorageBudgetError):
    """The declared estimate is absent, incomplete or self-contradictory."""

    reason_code = "ESTIMATE_UNAVAILABLE"


class FilesystemIdentityError(StorageBudgetError):
    """A candidate path could not be attributed to a measurable filesystem."""

    reason_code = "FILESYSTEM_MISIDENTIFIED"


class FencingError(StorageBudgetError):
    """A duplicate, stale or unproven owner generation tried to write."""

    reason_code = "FENCING_REJECTED"


class RenewalBoundError(StorageBudgetError):
    """A renewal tried to grow a reservation past its bounded ceiling."""

    reason_code = "RENEWAL_BOUND_EXCEEDED"


class AttributionUnavailableError(StorageBudgetError):
    """Observed bytes cannot be soundly attributed to the reservation."""

    reason_code = "ATTRIBUTION_UNAVAILABLE"


# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------


class AdmissionOutcome(StrEnum):
    """Result of one admission decision."""

    ADMITTED = "ADMITTED"
    EXCEEDED = "EXCEEDED"
    UNAVAILABLE = "UNAVAILABLE"


class EstimateSource(StrEnum):
    """Where a declared bound came from."""

    MEASURED = "MEASURED"
    HISTORICAL_UPPER_TAIL = "HISTORICAL_UPPER_TAIL"
    OWNER_HINT = "OWNER_HINT"
    CONSERVATIVE_FALLBACK = "CONSERVATIVE_FALLBACK"


class Attribution(StrEnum):
    """Whether consumed bytes can be charged to this reservation alone."""

    OWNED = "OWNED"
    SHARED_UNCERTAIN = "SHARED_UNCERTAIN"


class ReservationState(StrEnum):
    """Lifecycle of one reservation row."""

    RESERVED = "RESERVED"
    SETTLED = "SETTLED"
    RELEASED = "RELEASED"


ACTIVE_STATES: Final[frozenset[ReservationState]] = frozenset({ReservationState.RESERVED})


# --------------------------------------------------------------------------
# Filesystem identity
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FilesystemIdentity:
    """Stable identity of one physical filesystem, plus its live capacity.

    ``key`` must identify the filesystem rather than the path: same-device
    aliases and symlinked paths collapse onto one ``key``, and different
    devices stay independent.
    """

    key: str
    device: int | None
    total_bytes: int
    free_bytes: int

    def __post_init__(self) -> None:
        if not self.key:
            raise FilesystemIdentityError("filesystem identity has no key")
        if self.total_bytes < 0 or self.free_bytes < 0:
            raise FilesystemIdentityError("filesystem capacity cannot be negative")
        if self.free_bytes > self.total_bytes:
            raise FilesystemIdentityError(
                f"filesystem {self.key} reports free > total (misidentified)"
            )


class FilesystemProbe(Protocol):
    """Resolve an existing directory to its filesystem identity and capacity."""

    def __call__(self, path: Path) -> FilesystemIdentity: ...


def real_filesystem_identity(path: Path) -> FilesystemIdentity:
    """Measure ``path``'s real filesystem through ``stat``/``statvfs``.

    A missing path, a non-directory, an unreadable path or an implausible
    measurement fails closed: the caller must never fall back to "assume it
    fits".
    """
    try:
        info = os.stat(path)
    except OSError as exc:
        raise FilesystemIdentityError(f"cannot stat {path}: {exc}") from exc
    if not stat_module.S_ISDIR(info.st_mode):
        raise FilesystemIdentityError(f"not a directory: {path}")
    try:
        vfs = os.statvfs(path)
    except OSError as exc:
        raise FilesystemIdentityError(f"cannot statvfs {path}: {exc}") from exc
    frsize = int(vfs.f_frsize) or int(vfs.f_bsize)
    total = int(vfs.f_blocks) * frsize
    free = int(vfs.f_bavail) * frsize
    fsid = getattr(vfs, "f_fsid", None)
    key = f"{socket.gethostname()}:dev={int(info.st_dev)}:fsid={fsid!r}"
    return FilesystemIdentity(
        key=key, device=int(info.st_dev), total_bytes=total, free_bytes=free
    )


def _canonical_path(path: Path) -> str:
    """Return a canonical absolute path so aliases cannot double-count."""
    return os.path.realpath(os.path.abspath(str(path)))


# --------------------------------------------------------------------------
# Caller-declared estimate
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EstimateComponent:
    """One growing location inside a declared estimate.

    ``path`` is an existing directory whose filesystem will absorb the growth;
    ``remaining_growth_bytes`` counts *additional* bytes only, never bytes
    already on disk.
    """

    path: str
    remaining_growth_bytes: int
    uncertainty_bytes: int = 0
    label: str = ""

    def __post_init__(self) -> None:
        if not self.path.strip():
            raise EstimateUnavailableError("estimate component path is required")
        if self.remaining_growth_bytes < 0 or self.uncertainty_bytes < 0:
            raise EstimateUnavailableError("estimate component bytes cannot be negative")


@dataclass(frozen=True)
class StorageEstimate:
    """The Orchestrator's explicit, versioned bound for one work unit.

    The Orchestrator advises; deterministic software enforces.  Every field
    is validated: an incomplete or self-contradictory estimate is refused
    rather than read as zero.
    """

    components: tuple[EstimateComponent, ...]
    source: EstimateSource
    sample_count: int
    confidence: float
    estimate_version: int = ESTIMATE_VERSION
    conservative_fallback: bool = False
    attribution: Attribution = Attribution.OWNED
    reason: str = ""

    @property
    def bound_bytes(self) -> int:
        """Total declared remaining growth, uncertainty included."""
        return sum(c.remaining_growth_bytes + c.uncertainty_bytes for c in self.components)

    @property
    def uncertainty_bytes(self) -> int:
        """Total declared uncertainty buffer."""
        return sum(c.uncertainty_bytes for c in self.components)

    def validate(self) -> None:
        """Fail closed on an unknown, incomplete or contradictory estimate."""
        if not self.components:
            raise EstimateUnavailableError("estimate declares no components")
        if self.estimate_version < 1:
            raise EstimateUnavailableError(f"unknown estimate version {self.estimate_version}")
        if self.sample_count < 0:
            raise EstimateUnavailableError("sample_count cannot be negative")
        if not 0.0 <= self.confidence <= 1.0:
            raise EstimateUnavailableError(f"confidence out of range: {self.confidence}")
        if self.bound_bytes <= 0:
            raise EstimateUnavailableError(
                "a zero-byte bound reserves nothing and is never admissible"
            )
        if self.source in (EstimateSource.MEASURED, EstimateSource.HISTORICAL_UPPER_TAIL):
            if self.sample_count < 1:
                raise EstimateUnavailableError(
                    f"source {self.source} claims samples but declares none"
                )
            if self.confidence <= 0.0:
                raise EstimateUnavailableError(
                    f"source {self.source} claims samples but declares zero confidence"
                )
        if self.source is EstimateSource.CONSERVATIVE_FALLBACK and not self.conservative_fallback:
            raise EstimateUnavailableError(
                "source CONSERVATIVE_FALLBACK requires conservative_fallback=True"
            )


# --------------------------------------------------------------------------
# Reservation identity
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ReservationKey:
    """Idempotency key: installation / session / attempt / phase.

    Four independent sessions produce four keys; the same key declared twice is
    one reservation.  A shared parent budget (for example one wave gate) is
    declared once and referenced by later phases rather than re-declared per
    member, which is what keeps it from being counted four times.
    """

    installation_id: str
    session_id: str
    attempt_id: str
    phase_id: str

    def __post_init__(self) -> None:
        for name in ("installation_id", "session_id", "attempt_id", "phase_id"):
            value = getattr(self, name)
            if _KEY_PART_RE.fullmatch(value) is None:
                raise FencingError(f"invalid reservation key part {name}={value!r}")

    def render(self) -> str:
        """Return the audit-stable string form of this key."""
        return f"{self.installation_id}/{self.session_id}/{self.attempt_id}/{self.phase_id}"

    @property
    def reservation_id(self) -> str:
        """Deterministic primary key derived from :meth:`render`."""
        return hashlib.sha256(self.render().encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class OwnerToken:
    """Fencing token plus the process incarnation that owns the reservation."""

    owner_id: str
    generation: int
    identity: ProcessIdentity | None = None

    def resolved_identity(self) -> ProcessIdentity:
        """Return the recorded identity, defaulting to this process."""
        return self.identity if self.identity is not None else ProcessIdentity.for_self()


# --------------------------------------------------------------------------
# Recovery margin policy (owner settings matter)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MarginPolicy:
    """An owner-approved recovery margin, or an explicit absence of one."""

    approved: bool
    fixed_bytes: int | None
    fraction: float | None
    min_bytes: int
    max_bytes: int
    version: str
    source: str

    def margin_for(self, free_bytes: int, total_bytes: int) -> int:
        """Return the authorised margin for one filesystem.

        The margin is always positive and always bounded by ``max_bytes``: a
        nonzero recovery reserve is mandatory, but it is not the old fixed
        ``max(10 GiB, 10 %)`` floor.
        """
        del free_bytes  # the owner policy is anchored on total capacity
        if not self.approved:
            raise PolicyUnavailableError(
                "no owner-approved recovery-margin policy is recorded"
            )
        if self.fixed_bytes is not None:
            raw = self.fixed_bytes
        elif self.fraction is not None:
            raw = int(total_bytes * self.fraction)
        else:
            raise PolicyUnavailableError("margin policy declares no amount")
        margin = max(self.min_bytes, min(raw, self.max_bytes))
        if margin <= 0:
            raise PolicyUnavailableError("resolved recovery margin must be positive")
        return margin


def _unapproved(source: str) -> MarginPolicy:
    return MarginPolicy(
        approved=False,
        fixed_bytes=None,
        fraction=None,
        min_bytes=0,
        max_bytes=0,
        version="none",
        source=source,
    )


def load_margin_policy(path: Path | None) -> MarginPolicy:
    """Read the owner margin policy; an absent file is an unapproved policy.

    Malformed, ambiguous or non-positive values raise
    :class:`PolicyUnavailableError` so they can never be misread as "no
    reserve needed".  Nothing here invents a numeric default.
    """
    if path is None:
        return _unapproved("absent")
    if not path.exists():
        return _unapproved("absent")
    if path.is_symlink():
        raise LedgerSecurityError(f"margin policy path is a symlink: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PolicyUnavailableError(f"unreadable margin policy: {exc}") from exc
    if not isinstance(raw, dict):
        raise PolicyUnavailableError("margin policy must be a JSON object")
    approved = raw.get("approved")
    if not isinstance(approved, bool):
        raise PolicyUnavailableError("margin policy 'approved' must be a boolean")
    source = str(raw.get("source", "owner-file"))
    if not approved:
        return _unapproved(source)
    fixed = raw.get("fixed_bytes")
    fraction = raw.get("fraction")
    if (fixed is None) == (fraction is None):
        raise PolicyUnavailableError(
            "an approved policy declares exactly one of fixed_bytes or fraction"
        )
    min_bytes = raw.get("min_bytes")
    max_bytes = raw.get("max_bytes")
    if not isinstance(min_bytes, int) or not isinstance(max_bytes, int):
        raise PolicyUnavailableError("an approved policy requires integer min_bytes and max_bytes")
    if isinstance(min_bytes, bool) or isinstance(max_bytes, bool):
        raise PolicyUnavailableError("min_bytes and max_bytes must be integers")
    if min_bytes <= 0 or max_bytes <= 0 or min_bytes > max_bytes:
        raise PolicyUnavailableError("an approved policy requires 0 < min_bytes <= max_bytes")
    if fixed is not None:
        if isinstance(fixed, bool) or not isinstance(fixed, int) or fixed <= 0:
            raise PolicyUnavailableError("fixed_bytes must be a positive integer")
        resolved_fixed: int | None = fixed
        resolved_fraction: float | None = None
    else:
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            raise PolicyUnavailableError("fraction must be a number")
        value = float(fraction)
        if not 0.0 < value < 1.0:
            raise PolicyUnavailableError("fraction must be strictly between 0 and 1")
        resolved_fixed = None
        resolved_fraction = value
    return MarginPolicy(
        approved=True,
        fixed_bytes=resolved_fixed,
        fraction=resolved_fraction,
        min_bytes=min_bytes,
        max_bytes=max_bytes,
        version=str(raw.get("version", "1")),
        source=source,
    )


# --------------------------------------------------------------------------
# Ledger configuration and read-model records
# --------------------------------------------------------------------------


def canonical_ledger_root() -> Path:
    """Return the one authoritative host/user ledger root.

    Sharing is the point: every repository, project and CLI session of one
    user resolves the same root, so no configuration can silently instantiate
    an independent ledger.
    """
    return saberops_paths.owner_state_dir() / LEDGER_DIRNAME


def default_policy_path() -> Path:
    """Return the canonical owner settings path for the margin policy."""
    return saberops_paths.saberops_config_dir() / POLICY_FILENAME


@dataclass(frozen=True)
class LedgerConfig:
    """Where the shared ledger lives and how bounded its critical section is."""

    root: Path = field(default_factory=canonical_ledger_root)
    policy_path: Path | None = field(default_factory=default_policy_path)
    allow_isolated: bool = False
    lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS
    busy_timeout_seconds: float = DEFAULT_BUSY_TIMEOUT_SECONDS
    lease_seconds: float = DEFAULT_LEASE_SECONDS
    shadow_margin_bytes: int = SHADOW_MARGIN_BYTES

    def __post_init__(self) -> None:
        if self.lock_timeout_seconds <= 0:
            raise LedgerSecurityError("lock_timeout_seconds must be positive")
        if self.busy_timeout_seconds <= 0:
            raise LedgerSecurityError("busy_timeout_seconds must be positive")
        if self.lease_seconds <= 0:
            raise LedgerSecurityError("lease_seconds must be positive")
        if self.shadow_margin_bytes <= 0:
            raise LedgerSecurityError("shadow_margin_bytes must be positive")

    @property
    def database_path(self) -> Path:
        """Absolute ledger database path."""
        return self.root / LEDGER_FILENAME

    @property
    def lock_path(self) -> Path:
        """Absolute host-wide admission lock path."""
        return self.root / LOCK_FILENAME


@dataclass(frozen=True)
class FsSample:
    """One filesystem line of one reservation."""

    fs_key: str
    bound_bytes: int
    original_bound_bytes: int
    observed_bytes: int
    overshoot_bytes: int
    attribution: Attribution

    @property
    def remaining_bytes(self) -> int:
        """Conservative remaining future growth charged to this filesystem."""
        if self.attribution is Attribution.SHARED_UNCERTAIN:
            return self.bound_bytes
        return max(0, self.bound_bytes - self.observed_bytes)


@dataclass(frozen=True)
class Reservation:
    """One durable reservation row plus its per-filesystem bounds."""

    key: ReservationKey
    owner_id: str
    owner_generation: int
    owner_identity: ProcessIdentity | None
    state: ReservationState
    estimate_source: EstimateSource
    estimate_version: int
    sample_count: int
    confidence: float
    reason: str
    created_at: str
    renewed_at: str
    lease_expires_at: str
    filesystems: tuple[FsSample, ...]

    @property
    def reservation_id(self) -> str:
        """Deterministic identifier of this reservation."""
        return self.key.reservation_id


@dataclass(frozen=True)
class Contribution:
    """One reservation's remaining liability on one filesystem."""

    reservation: Reservation
    fs_key: str
    remaining_growth_bytes: int
    stale: bool
    stale_reason: str
    backpressure: bool


@dataclass(frozen=True)
class FsBudget:
    """Deterministic per-filesystem arithmetic behind one decision."""

    fs_key: str
    device: int | None
    total_bytes: int
    free_bytes: int
    emergency_margin: int
    total_active_remaining: int
    candidate_bound: int
    required_free: int
    available_for_new_admissions: int
    fits: bool
    contributions: tuple[Contribution, ...]


@dataclass(frozen=True)
class StorageDecision:
    """Deterministic, reasoned admission result; never a model call."""

    outcome: AdmissionOutcome
    reason_code: str
    reason: str
    filesystems: tuple[FsBudget, ...]
    estimate_version: int
    confidence: float
    margin_authorised: bool
    margin_source: str
    reserved: bool
    backpressure: tuple[str, ...] = ()
    model_calls: int = MODEL_CALLS

    @property
    def admitted(self) -> bool:
        """True only when the candidate holds a durable reservation."""
        return self.outcome is AdmissionOutcome.ADMITTED

    @property
    def primary(self) -> FsBudget | None:
        """The binding filesystem: the smallest candidate headroom, ties by key.

        Cross-filesystem totals are deliberately not aggregated; independent
        devices are reported and compared separately.  The binding filesystem
        is the one that decides this admission.
        """
        if not self.filesystems:
            return None
        return min(
            self.filesystems,
            key=lambda budget: (
                budget.available_for_new_admissions - budget.candidate_bound,
                budget.fs_key,
            ),
        )

    @property
    def free_bytes(self) -> int | None:
        """Live free bytes of the binding filesystem, or ``None``."""
        return None if self.primary is None else self.primary.free_bytes

    @property
    def emergency_margin(self) -> int | None:
        """Recovery margin of the binding filesystem, or ``None``."""
        return None if self.primary is None else self.primary.emergency_margin

    @property
    def total_active_remaining(self) -> int | None:
        """Sum of admitted remaining growth on the binding filesystem."""
        return None if self.primary is None else self.primary.total_active_remaining

    @property
    def candidate_bound(self) -> int | None:
        """Candidate bound on the binding filesystem, or ``None``."""
        return None if self.primary is None else self.primary.candidate_bound

    @property
    def required_free(self) -> int | None:
        """Margin + active remaining + candidate on the binding filesystem."""
        return None if self.primary is None else self.primary.required_free

    @property
    def available_for_new_admissions(self) -> int | None:
        """Free minus margin minus active remaining, on the binding filesystem."""
        return None if self.primary is None else self.primary.available_for_new_admissions

    def as_dict(self) -> dict[str, object]:
        """Return a JSON-safe deterministic report of this decision."""
        return {
            "outcome": str(self.outcome),
            "reason_code": self.reason_code,
            "reason": self.reason,
            "reserved": self.reserved,
            "margin_authorised": self.margin_authorised,
            "margin_source": self.margin_source,
            "estimate_version": self.estimate_version,
            "confidence": self.confidence,
            "backpressure": list(self.backpressure),
            "model_calls": self.model_calls,
            "free_bytes": self.free_bytes,
            "emergency_margin": self.emergency_margin,
            "total_active_remaining": self.total_active_remaining,
            "candidate_bound": self.candidate_bound,
            "required_free": self.required_free,
            "available_for_new_admissions": self.available_for_new_admissions,
            "filesystems": [
                {
                    "fs_key": budget.fs_key,
                    "device": budget.device,
                    "total_bytes": budget.total_bytes,
                    "free_bytes": budget.free_bytes,
                    "emergency_margin": budget.emergency_margin,
                    "total_active_remaining": budget.total_active_remaining,
                    "candidate_bound": budget.candidate_bound,
                    "required_free": budget.required_free,
                    "available_for_new_admissions": budget.available_for_new_admissions,
                    "fits": budget.fits,
                    "contributions": [
                        {
                            "reservation": item.reservation.key.render(),
                            "remaining_growth_bytes": item.remaining_growth_bytes,
                            "stale": item.stale,
                            "stale_reason": item.stale_reason,
                            "backpressure": item.backpressure,
                        }
                        for item in budget.contributions
                    ],
                }
                for budget in self.filesystems
            ],
        }


@dataclass(frozen=True)
class ReservationEvent:
    """One append-only audit event for a reservation."""

    reservation_id: str
    at: str
    event: str
    owner_generation: int
    detail: str


_T = TypeVar("_T")

#: Ledger DDL, applied atomically on first use.
_SCHEMA_STATEMENTS: Final[tuple[str, ...]] = (
    """
    CREATE TABLE IF NOT EXISTS ledger_meta (
        name TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reservations (
        res_id TEXT PRIMARY KEY,
        installation_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL,
        phase_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        owner_generation INTEGER NOT NULL,
        owner_identity TEXT NOT NULL,
        attribution TEXT NOT NULL,
        estimate_source TEXT NOT NULL,
        estimate_version INTEGER NOT NULL,
        sample_count INTEGER NOT NULL,
        confidence REAL NOT NULL,
        reason TEXT NOT NULL,
        state TEXT NOT NULL,
        created_at TEXT NOT NULL,
        renewed_at TEXT NOT NULL,
        lease_expires_at TEXT NOT NULL,
        settled_at TEXT,
        released_at TEXT,
        release_reason TEXT,
        UNIQUE (installation_id, session_id, attempt_id, phase_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reservation_filesystems (
        res_id TEXT NOT NULL,
        fs_key TEXT NOT NULL,
        bound_bytes INTEGER NOT NULL,
        original_bound_bytes INTEGER NOT NULL,
        observed_bytes INTEGER NOT NULL,
        attribution TEXT NOT NULL,
        PRIMARY KEY (res_id, fs_key),
        FOREIGN KEY (res_id) REFERENCES reservations(res_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS reservation_events (
        res_id TEXT NOT NULL,
        at TEXT NOT NULL,
        event TEXT NOT NULL,
        owner_generation INTEGER NOT NULL,
        detail TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS reservation_events_res ON reservation_events(res_id, at)",
)


def _utcnow(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return _utcnow(moment).isoformat(timespec="seconds")


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw).astimezone(UTC)
    except ValueError:
        return None


def compute_required_free(emergency_margin: int, active_remaining: int, candidate: int) -> int:
    """Return the canonical per-filesystem requirement.

    Covers: ADR-0003 Amendment 2 (``required = margin + sum(active) + candidate``).
    """
    if min(emergency_margin, active_remaining, candidate) < 0:
        raise EstimateUnavailableError("negative byte component in the storage requirement")
    return emergency_margin + active_remaining + candidate


def _make_decision(
    outcome: AdmissionOutcome,
    reason_code: str,
    reason: str,
    filesystems: tuple[FsBudget, ...],
    estimate: StorageEstimate,
    *,
    margin_authorised: bool,
    margin_source: str,
    reserved: bool = False,
    backpressure: tuple[str, ...] = (),
) -> StorageDecision:
    """Build a decision with every reported field explicit and typed."""
    return StorageDecision(
        outcome=outcome,
        reason_code=reason_code,
        reason=reason,
        filesystems=filesystems,
        estimate_version=estimate.estimate_version,
        confidence=estimate.confidence,
        margin_authorised=margin_authorised,
        margin_source=margin_source,
        reserved=reserved,
        backpressure=backpressure,
    )


class StorageLedger:
    """The one host/user authoritative reservation ledger and lock.

    Construct with an explicit :class:`LedgerConfig` and optional
    :class:`FilesystemProbe` (tests inject a fake filesystem).  Public methods
    are safe to call from several threads or processes: every read/modify/write
    happens inside one bounded ``flock`` critical section and one
    ``BEGIN IMMEDIATE`` transaction.
    """

    def __init__(self, config: LedgerConfig, probe: FilesystemProbe | None = None) -> None:
        self._config = config
        self._probe: FilesystemProbe = probe or real_filesystem_identity
        self._root = config.root
        self._opened = False

    def __enter__(self) -> StorageLedger:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- lifecycle ---------------------------------------------------------

    @property
    def config(self) -> LedgerConfig:
        """The ledger configuration (read-only)."""
        return self._config

    def open(self) -> None:
        """Create or validate the ledger, then mark it usable.

        Creating is idempotent; validating never "repairs" a mismatch back
        into service, because a wrong scope or schema proves the file is not
        the shared ledger this process is entitled to write.
        """
        if self._opened:
            return
        self._check_shared_scope()
        self._secure_root()
        self._ensure_files()
        fd = self._acquire_lock()
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._create_schema(conn)
                self._verify_meta(conn)
                conn.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
        except sqlite3.OperationalError as exc:
            raise LedgerUnavailableError(f"cannot initialise ledger: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            raise LedgerCorruptError(f"ledger is not a usable database: {exc}") from exc
        except sqlite3.Error as exc:
            raise LedgerUnavailableError(f"cannot initialise ledger: {exc}") from exc
        finally:
            if conn is not None:
                conn.close()
            self._release_lock(fd)
        self._harden_files()
        self._opened = True

    def close(self) -> None:
        """Release local state; the ledger itself keeps every record."""
        self._opened = False

    def _check_shared_scope(self) -> None:
        if self._config.allow_isolated:
            return
        canonical = os.path.realpath(str(canonical_ledger_root()))
        actual = os.path.realpath(str(self._root))
        if actual != canonical:
            raise LedgerSecurityError(
                f"independent storage ledger rejected: {actual} is not the shared root "
                f"{canonical} (allow_isolated is for hermetic tests only)"
            )

    def _secure_root(self) -> None:
        root = self._root
        if root.is_symlink():
            raise LedgerSecurityError(f"ledger root is a symlink: {root}")
        try:
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise LedgerUnavailableError(f"cannot create ledger root {root}: {exc}") from exc
        info = root.stat()
        if not stat_module.S_ISDIR(info.st_mode):
            raise LedgerSecurityError(f"ledger root is not a directory: {root}")
        if info.st_uid != os.geteuid():
            raise LedgerSecurityError(
                f"ledger root is not owned by this user: {root} (uid {info.st_uid})"
            )
        mode = stat_module.S_IMODE(info.st_mode)
        if mode & 0o077:
            os.chmod(root, mode & ~0o077)

    def _ensure_files(self) -> None:
        for path in (self._config.database_path, self._config.lock_path):
            if path.is_symlink():
                raise LedgerSecurityError(f"ledger file is a symlink: {path}")

    def _harden_files(self) -> None:
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self._config.database_path) + suffix)
            if path.exists() and not path.is_symlink():
                with contextlib.suppress(OSError):
                    os.chmod(path, 0o600)

    # -- locking and connection -------------------------------------------

    def _acquire_lock(self) -> int:
        try:
            fd = os.open(
                self._config.lock_path,
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
        except OSError as exc:
            raise LedgerUnavailableError(f"cannot open admission lock: {exc}") from exc
        deadline = time.monotonic() + self._config.lock_timeout_seconds
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockContentionError(
                        "timed out waiting for the host-wide storage admission lock"
                    ) from None
                time.sleep(0.005)
            except OSError as exc:
                os.close(fd)
                raise LedgerUnavailableError(f"cannot take admission lock: {exc}") from exc

    def _release_lock(self, fd: int) -> None:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                self._config.database_path,
                timeout=self._config.busy_timeout_seconds,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {int(self._config.busy_timeout_seconds * 1000)}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA foreign_keys = ON")
        except sqlite3.OperationalError as exc:
            raise LedgerUnavailableError(f"cannot open ledger database: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            raise LedgerCorruptError(f"ledger database is corrupt: {exc}") from exc
        return conn

    def _transaction(self, action: Callable[[sqlite3.Connection], _T]) -> _T:
        self.open()
        fd = self._acquire_lock()
        conn: sqlite3.Connection | None = None
        try:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                result = action(conn)
                conn.execute("COMMIT")
                return result
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                raise
        except sqlite3.OperationalError as exc:
            raise LedgerUnavailableError(f"ledger transaction failed: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            raise LedgerCorruptError(f"ledger is corrupt: {exc}") from exc
        except sqlite3.Error as exc:
            raise LedgerUnavailableError(f"ledger transaction failed: {exc}") from exc
        finally:
            if conn is not None:
                conn.close()
            self._release_lock(fd)

    # -- schema ------------------------------------------------------------

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        """Create the schema with plain statements so the caller's transaction holds.

        ``executescript`` implicitly commits, which would break the atomic
        first-use initialisation and could leave a half-written meta block.
        """
        for statement in _SCHEMA_STATEMENTS:
            conn.execute(statement)

    def _verify_meta(self, conn: sqlite3.Connection) -> None:
        meta: dict[str, str] = {}
        for row in conn.execute("SELECT name, value FROM ledger_meta").fetchall():
            meta[str(row["name"])] = str(row["value"])
        if not meta:
            expected = os.path.realpath(str(self._root))
            for name, value in (
                ("schema_version", str(SCHEMA_VERSION)),
                ("ledger_scope", expected),
                ("created_at", datetime.now(UTC).isoformat(timespec="seconds")),
                ("host", socket.gethostname()),
            ):
                conn.execute(
                    "INSERT INTO ledger_meta (name, value) VALUES (?, ?)", (name, value)
                )
            return
        recorded_schema = meta.get("schema_version")
        if recorded_schema != str(SCHEMA_VERSION):
            raise LedgerCorruptError(
                f"ledger schema_version {recorded_schema!r} != {SCHEMA_VERSION}"
            )
        expected = os.path.realpath(str(self._root))
        recorded_scope = meta.get("ledger_scope")
        if recorded_scope != expected:
            raise LedgerSecurityError(
                f"ledger scope {recorded_scope!r} does not match {expected!r} "
                "(path redirection or a copied ledger)"
            )

    # -- rows --------------------------------------------------------------

    def _event(
        self, conn: sqlite3.Connection, res_id: str, event: str, generation: int, detail: str
    ) -> None:
        conn.execute(
            "INSERT INTO reservation_events (res_id, at, event, owner_generation, detail)"
            " VALUES (?, ?, ?, ?, ?)",
            (res_id, datetime.now(UTC).isoformat(timespec="seconds"), event, generation, detail),
        )

    def _load_samples(self, conn: sqlite3.Connection, res_id: str) -> tuple[FsSample, ...]:
        samples: list[FsSample] = []
        for row in conn.execute(
            "SELECT fs_key, bound_bytes, original_bound_bytes, observed_bytes, attribution"
            " FROM reservation_filesystems WHERE res_id = ? ORDER BY fs_key",
            (res_id,),
        ).fetchall():
            samples.append(
                FsSample(
                    fs_key=str(row["fs_key"]),
                    bound_bytes=int(row["bound_bytes"]),
                    original_bound_bytes=int(row["original_bound_bytes"]),
                    observed_bytes=int(row["observed_bytes"]),
                    overshoot_bytes=max(0, int(row["observed_bytes"]) - int(row["bound_bytes"])),
                    attribution=Attribution(str(row["attribution"])),
                )
            )
        return tuple(samples)

    def _row_to_reservation(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> Reservation:
        return Reservation(
            key=ReservationKey(
                installation_id=str(row["installation_id"]),
                session_id=str(row["session_id"]),
                attempt_id=str(row["attempt_id"]),
                phase_id=str(row["phase_id"]),
            ),
            owner_id=str(row["owner_id"]),
            owner_generation=int(row["owner_generation"]),
            owner_identity=ProcessIdentity.from_json(str(row["owner_identity"])),
            state=ReservationState(str(row["state"])),
            estimate_source=EstimateSource(str(row["estimate_source"])),
            estimate_version=int(row["estimate_version"]),
            sample_count=int(row["sample_count"]),
            confidence=float(row["confidence"]),
            reason=str(row["reason"]),
            created_at=str(row["created_at"]),
            renewed_at=str(row["renewed_at"]),
            lease_expires_at=str(row["lease_expires_at"]),
            filesystems=self._load_samples(conn, str(row["res_id"])),
        )

    def _load_by_key(
        self, conn: sqlite3.Connection, key: ReservationKey
    ) -> Reservation | None:
        row = conn.execute(
            "SELECT * FROM reservations WHERE res_id = ?", (key.reservation_id,)
        ).fetchone()
        if row is None:
            return None
        return self._row_to_reservation(conn, row)

    def _load_active(self, conn: sqlite3.Connection) -> tuple[Reservation, ...]:
        out: list[Reservation] = []
        for row in conn.execute(
            "SELECT * FROM reservations WHERE state = ? ORDER BY session_id, attempt_id, phase_id",
            (ReservationState.RESERVED.value,),
        ).fetchall():
            out.append(self._row_to_reservation(conn, row))
        return tuple(out)

    # -- staleness ---------------------------------------------------------

    def _staleness(self, reservation: Reservation, moment: datetime) -> tuple[bool, str]:
        """Decide whether an active reservation looks abandoned.

        Stale rows are still charged in full: an unproven owner never shrinks a
        reservation.  Only :meth:`reclaim_stale` may retire one, and only on
        positive proof of death plus an expired lease.
        """
        expiry = _parse_iso(reservation.lease_expires_at)
        lease_expired = expiry is not None and moment >= expiry
        liveness = probe_liveness(reservation.owner_identity)
        if liveness is Liveness.DEAD and lease_expired:
            return True, "owner_dead_lease_expired"
        if liveness is Liveness.DEAD:
            return True, "owner_dead_lease_active"
        if lease_expired:
            return True, "lease_expired_owner_unproven"
        return False, ""

    def _contributions(
        self, conn: sqlite3.Connection, moment: datetime
    ) -> tuple[Contribution, ...]:
        items: list[Contribution] = []
        for reservation in self._load_active(conn):
            stale, stale_reason = self._staleness(reservation, moment)
            for sample in reservation.filesystems:
                items.append(
                    Contribution(
                        reservation=reservation,
                        fs_key=sample.fs_key,
                        remaining_growth_bytes=sample.remaining_bytes,
                        stale=stale,
                        stale_reason=stale_reason,
                        backpressure=sample.overshoot_bytes > 0,
                    )
                )
        return tuple(items)

    # -- decision arithmetic ----------------------------------------------

    def _resolve_candidates(
        self, estimate: StorageEstimate
    ) -> tuple[dict[str, int], dict[str, FilesystemIdentity]]:
        """Group a candidate's components by real filesystem.

        Components that canonicalise to the same path are aliases and count
        once (the larger declaration); distinct paths on the same filesystem
        are distinct growth locations and add up.
        """
        per_path: dict[str, tuple[int, FilesystemIdentity]] = {}
        for component in estimate.components:
            path = Path(component.path)
            identity = self._probe(path)
            canonical = _canonical_path(path)
            amount = component.remaining_growth_bytes + component.uncertainty_bytes
            previous = per_path.get(canonical)
            if previous is None or amount > previous[0]:
                per_path[canonical] = (amount, identity)
        totals: dict[str, int] = {}
        identities: dict[str, FilesystemIdentity] = {}
        for amount, identity in per_path.values():
            totals[identity.key] = totals.get(identity.key, 0) + amount
            prior_identity = identities.get(identity.key)
            if prior_identity is None or identity.free_bytes < prior_identity.free_bytes:
                identities[identity.key] = identity
        return totals, identities

    def _margin(self, policy: MarginPolicy, identity: FilesystemIdentity) -> tuple[int, str]:
        if policy.approved:
            try:
                return policy.margin_for(identity.free_bytes, identity.total_bytes), policy.source
            except PolicyUnavailableError:
                pass
        return self._config.shadow_margin_bytes, "shadow-placeholder-unapproved"

    def _assess(
        self, conn: sqlite3.Connection, estimate: StorageEstimate, moment: datetime
    ) -> StorageDecision:
        estimate.validate()
        policy = load_margin_policy(self._config.policy_path)
        candidate_totals, identities = self._resolve_candidates(estimate)
        if not candidate_totals:
            raise EstimateUnavailableError("estimate resolves to no filesystem growth")
        contributions = self._contributions(conn, moment)
        budgets: list[FsBudget] = []
        backpressure: list[str] = []
        margin_source = ""
        for fs_key in sorted(identities):
            identity = identities[fs_key]
            margin, margin_source = self._margin(policy, identity)
            rows = tuple(item for item in contributions if item.fs_key == fs_key)
            active = sum(item.remaining_growth_bytes for item in rows)
            candidate = candidate_totals[fs_key]
            required = compute_required_free(margin, active, candidate)
            available = max(0, identity.free_bytes - margin - active)
            if any(item.backpressure for item in rows):
                backpressure.append(fs_key)
            budgets.append(
                FsBudget(
                    fs_key=fs_key,
                    device=identity.device,
                    total_bytes=identity.total_bytes,
                    free_bytes=identity.free_bytes,
                    emergency_margin=margin,
                    total_active_remaining=active,
                    candidate_bound=candidate,
                    required_free=required,
                    available_for_new_admissions=available,
                    fits=candidate <= available,
                    contributions=rows,
                )
            )
        filesystems = tuple(budgets)
        if not policy.approved:
            return _make_decision(
                AdmissionOutcome.UNAVAILABLE,
                PolicyUnavailableError.reason_code,
                "no owner-approved recovery-margin policy; shadow estimate only",
                filesystems,
                estimate,
                margin_authorised=False,
                margin_source=margin_source,
                backpressure=tuple(backpressure),
            )
        if backpressure:
            return _make_decision(
                AdmissionOutcome.UNAVAILABLE,
                "BACKPRESSURE",
                "observed growth exceeded a declared bound on "
                + ", ".join(backpressure)
                + "; new starts paused until the estimate is renewed",
                filesystems,
                estimate,
                margin_authorised=True,
                margin_source=margin_source,
                backpressure=tuple(backpressure),
            )
        if not all(budget.fits for budget in filesystems):
            failing = sorted(budget.fs_key for budget in filesystems if not budget.fits)
            return _make_decision(
                AdmissionOutcome.EXCEEDED,
                REASON_EXCEEDED,
                "candidate does not fit on " + ", ".join(failing),
                filesystems,
                estimate,
                margin_authorised=True,
                margin_source=margin_source,
            )
        return _make_decision(
            AdmissionOutcome.ADMITTED,
            REASON_OK,
            "reservation fits on every touched filesystem",
            filesystems,
            estimate,
            margin_authorised=True,
            margin_source=margin_source,
        )

    def _unavailable(self, estimate: StorageEstimate, error: StorageBudgetError) -> StorageDecision:
        return _make_decision(
            AdmissionOutcome.UNAVAILABLE,
            error.reason_code,
            str(error),
            (),
            estimate,
            margin_authorised=False,
            margin_source="unavailable",
        )

    # -- public API --------------------------------------------------------

    def assess(self, estimate: StorageEstimate, *, now: datetime | None = None) -> StorageDecision:
        """Measure and compare without writing; the shadow/report path.

        Never reserves anything, and never admits: the ledger is untouched.
        """
        moment = _utcnow(now)
        try:
            return self._transaction(lambda conn: self._assess(conn, estimate, moment))
        except StorageBudgetError as exc:
            return self._unavailable(estimate, exc)
        except OSError as exc:
            return self._unavailable(estimate, LedgerUnavailableError(str(exc)))

    def try_reserve(
        self,
        key: ReservationKey,
        owner: OwnerToken,
        estimate: StorageEstimate,
        *,
        now: datetime | None = None,
    ) -> StorageDecision:
        """Atomically admit and durably commit one reservation.

        The caller must not start any expensive work (process, venv, gate)
        until this returns ``ADMITTED``: the reservation is committed under
        ``synchronous=FULL`` before the method returns.
        """
        moment = _utcnow(now)
        try:
            return self._transaction(
                lambda conn: self._reserve_locked(conn, key, owner, estimate, moment)
            )
        except StorageBudgetError as exc:
            return self._unavailable(estimate, exc)
        except OSError as exc:
            return self._unavailable(estimate, LedgerUnavailableError(str(exc)))

    def _reserve_locked(
        self,
        conn: sqlite3.Connection,
        key: ReservationKey,
        owner: OwnerToken,
        estimate: StorageEstimate,
        moment: datetime,
    ) -> StorageDecision:
        decision = self._assess(conn, estimate, moment)
        if decision.outcome is not AdmissionOutcome.ADMITTED:
            return decision
        candidate_totals = {
            budget.fs_key: budget.candidate_bound for budget in decision.filesystems
        }
        existing = self._load_by_key(conn, key)
        if existing is not None and existing.state is ReservationState.RESERVED:
            # Admission is also the recovery path: a restarted attempt may adopt
            # its own orphaned reservation once the previous incarnation is
            # proven dead, so a SIGKILL between commit and start cannot strand it.
            self._renew_locked(
                conn, existing, owner, estimate, candidate_totals, moment, takeover=True
            )
            return replace(decision, reserved=True)
        self._insert_locked(conn, key, owner, estimate, candidate_totals, moment, existing)
        return replace(decision, reserved=True)

    def _insert_locked(
        self,
        conn: sqlite3.Connection,
        key: ReservationKey,
        owner: OwnerToken,
        estimate: StorageEstimate,
        candidate_totals: dict[str, int],
        moment: datetime,
        existing: Reservation | None,
    ) -> None:
        identity = owner.resolved_identity()
        identity_json = json.dumps(identity.to_dict(), sort_keys=True)
        created = _iso(moment)
        expiry = _iso(moment + timedelta(seconds=self._config.lease_seconds))
        res_id = key.reservation_id
        if existing is not None:
            conn.execute(
                "UPDATE reservations SET owner_id = ?, owner_generation = ?, owner_identity = ?,"
                " attribution = ?, estimate_source = ?, estimate_version = ?, sample_count = ?,"
                " confidence = ?, reason = ?, state = ?, created_at = ?, renewed_at = ?,"
                " lease_expires_at = ?, settled_at = NULL, released_at = NULL,"
                " release_reason = NULL WHERE res_id = ?",
                (
                    owner.owner_id,
                    owner.generation,
                    identity_json,
                    estimate.attribution.value,
                    estimate.source.value,
                    estimate.estimate_version,
                    estimate.sample_count,
                    estimate.confidence,
                    estimate.reason,
                    ReservationState.RESERVED.value,
                    created,
                    created,
                    expiry,
                    res_id,
                ),
            )
            conn.execute("DELETE FROM reservation_filesystems WHERE res_id = ?", (res_id,))
        else:
            conn.execute(
                "INSERT INTO reservations (res_id, installation_id, session_id, attempt_id,"
                " phase_id, owner_id, owner_generation, owner_identity, attribution,"
                " estimate_source, estimate_version, sample_count, confidence, reason, state,"
                " created_at, renewed_at, lease_expires_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    res_id,
                    key.installation_id,
                    key.session_id,
                    key.attempt_id,
                    key.phase_id,
                    owner.owner_id,
                    owner.generation,
                    identity_json,
                    estimate.attribution.value,
                    estimate.source.value,
                    estimate.estimate_version,
                    estimate.sample_count,
                    estimate.confidence,
                    estimate.reason,
                    ReservationState.RESERVED.value,
                    created,
                    created,
                    expiry,
                ),
            )
        self._apply_bounds(
            conn,
            res_id,
            existing,
            candidate_totals,
            estimate.attribution,
            reset_observed=True,
        )
        self._event(conn, res_id, "RESERVED", owner.generation, key.render())

    def _apply_bounds(
        self,
        conn: sqlite3.Connection,
        res_id: str,
        existing: Reservation | None,
        candidate_totals: dict[str, int],
        attribution: Attribution,
        *,
        reset_observed: bool,
    ) -> None:
        """Write a reservation's per-filesystem bounds, bounded and lossless.

        A filesystem kept from a previous declaration preserves its original
        ceiling and (unless the estimate version changed) its observed bytes; a
        new one starts at the declared bound; one no longer declared is dropped
        so it cannot stay charged forever.  A new estimate version is a
        recalculation, so it starts the observed accumulator afresh.  Any raise
        here rolls the surrounding transaction back.
        """
        previous = {} if existing is None else {s.fs_key: s for s in existing.filesystems}
        for fs_key, bound in sorted(candidate_totals.items()):
            prior = previous.get(fs_key)
            original = prior.original_bound_bytes if prior is not None else bound
            if bound > original * MAX_RENEWAL_FACTOR:
                raise RenewalBoundError(
                    f"renewal of {res_id} on {fs_key} exceeds the bounded ceiling "
                    f"({bound} > {MAX_RENEWAL_FACTOR} x {original}); new estimate version needed"
                )
            observed = 0 if prior is None or reset_observed else prior.observed_bytes
            conn.execute(
                "INSERT INTO reservation_filesystems (res_id, fs_key, bound_bytes,"
                " original_bound_bytes, observed_bytes, attribution) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(res_id, fs_key) DO UPDATE SET"
                " bound_bytes = excluded.bound_bytes, observed_bytes = excluded.observed_bytes,"
                " attribution = excluded.attribution",
                (res_id, fs_key, bound, original, observed, attribution.value),
            )
        for fs_key in sorted(previous):
            if fs_key not in candidate_totals:
                conn.execute(
                    "DELETE FROM reservation_filesystems WHERE res_id = ? AND fs_key = ?",
                    (res_id, fs_key),
                )

    def _renew_locked(
        self,
        conn: sqlite3.Connection,
        existing: Reservation,
        owner: OwnerToken,
        estimate: StorageEstimate,
        candidate_totals: dict[str, int],
        moment: datetime,
        *,
        takeover: bool,
    ) -> None:
        self._authorise_owner(conn, existing, owner, moment, takeover=takeover)
        res_id = existing.reservation_id
        expiry = _iso(moment + timedelta(seconds=self._config.lease_seconds))
        conn.execute(
            "UPDATE reservations SET owner_id = ?, owner_generation = ?, owner_identity = ?,"
            " renewed_at = ?, lease_expires_at = ?, estimate_version = ? WHERE res_id = ?",
            (
                owner.owner_id,
                owner.generation,
                json.dumps(owner.resolved_identity().to_dict(), sort_keys=True),
                _iso(moment),
                expiry,
                estimate.estimate_version,
                res_id,
            ),
        )
        self._apply_bounds(
            conn,
            res_id,
            existing,
            candidate_totals,
            estimate.attribution,
            reset_observed=estimate.estimate_version != existing.estimate_version,
        )
        self._event(conn, res_id, "RENEWED", owner.generation, existing.key.render())

    def _authorise_owner(
        self,
        conn: sqlite3.Connection,
        existing: Reservation,
        owner: OwnerToken,
        moment: datetime,
        *,
        takeover: bool,
    ) -> None:
        """Fence a write: same incarnation, or a proven-dead predecessor.

        A stale generation, a duplicate claim, a live or unprovable owner
        refuses.  PID reuse is caught by the recorded process identity, not by
        the PID alone.
        """
        if owner.owner_id == existing.owner_id and owner.generation == existing.owner_generation:
            return
        if owner.generation <= existing.owner_generation:
            raise FencingError(
                f"stale or duplicate owner generation {owner.generation} for "
                f"{existing.reservation_id} (current {existing.owner_generation})"
            )
        if not takeover:
            raise FencingError(
                "a different owner may not renew this reservation; release or settle it first"
            )
        liveness = probe_liveness(existing.owner_identity)
        if liveness is not Liveness.DEAD:
            raise FencingError(
                f"takeover refused for {existing.reservation_id}: previous owner is {liveness}"
            )
        expiry = _parse_iso(existing.lease_expires_at)
        if expiry is None or moment < expiry:
            raise FencingError(
                f"takeover refused for {existing.reservation_id}: lease has not expired"
            )
        self._event(
            conn,
            existing.reservation_id,
            "TAKEOVER",
            owner.generation,
            f"previous owner generation {existing.owner_generation}",
        )

    def renew(
        self,
        key: ReservationKey,
        owner: OwnerToken,
        estimate: StorageEstimate,
        *,
        now: datetime | None = None,
    ) -> StorageDecision:
        """Re-declare a live reservation's bound, bounded and fit-checked.

        Lowering is free.  Raising is allowed only up to
        ``MAX_RENEWAL_FACTOR`` times the bound recorded at creation and only
        when the larger bound still fits; otherwise the previous bound stands
        and the caller gets a typed refusal.  A running job is never killed
        because a bound fell.
        """
        moment = _utcnow(now)
        try:
            return self._transaction(
                lambda conn: self._renew_public(conn, key, owner, estimate, moment)
            )
        except StorageBudgetError as exc:
            return self._unavailable(estimate, exc)
        except OSError as exc:
            return self._unavailable(estimate, LedgerUnavailableError(str(exc)))

    def _renew_public(
        self,
        conn: sqlite3.Connection,
        key: ReservationKey,
        owner: OwnerToken,
        estimate: StorageEstimate,
        moment: datetime,
    ) -> StorageDecision:
        estimate.validate()
        existing = self._load_by_key(conn, key)
        if existing is None or existing.state is not ReservationState.RESERVED:
            raise FencingError(f"no live reservation for {key.render()}")
        self._authorise_owner(conn, existing, owner, moment, takeover=False)
        policy = load_margin_policy(self._config.policy_path)
        if not policy.approved:
            raise PolicyUnavailableError("no owner-approved recovery-margin policy")
        candidate_totals, identities = self._resolve_candidates(estimate)
        contributions = self._contributions(conn, moment)
        budgets: list[FsBudget] = []
        for fs_key in sorted(identities):
            identity = identities[fs_key]
            margin, _ = self._margin(policy, identity)
            candidate = candidate_totals[fs_key]
            active = sum(
                item.remaining_growth_bytes
                for item in contributions
                if item.fs_key == fs_key
                and item.reservation.reservation_id != existing.reservation_id
            )
            available = max(0, identity.free_bytes - margin - active)
            budgets.append(
                FsBudget(
                    fs_key=fs_key,
                    device=identity.device,
                    total_bytes=identity.total_bytes,
                    free_bytes=identity.free_bytes,
                    emergency_margin=margin,
                    total_active_remaining=active,
                    candidate_bound=candidate,
                    required_free=compute_required_free(margin, active, candidate),
                    available_for_new_admissions=available,
                    fits=candidate <= available,
                    contributions=tuple(
                        item for item in contributions if item.fs_key == fs_key
                    ),
                )
            )
        if not all(budget.fits for budget in budgets):
            return _make_decision(
                AdmissionOutcome.EXCEEDED,
                REASON_EXCEEDED,
                "renewal does not fit; the previous bound stands",
                tuple(budgets),
                estimate,
                margin_authorised=True,
                margin_source=policy.source,
            )
        self._renew_locked(
            conn, existing, owner, estimate, candidate_totals, moment, takeover=False
        )
        return _make_decision(
            AdmissionOutcome.ADMITTED,
            REASON_OK,
            "renewal accepted",
            tuple(budgets),
            estimate,
            margin_authorised=True,
            margin_source=policy.source,
            reserved=True,
        )

    def observe(
        self,
        key: ReservationKey,
        owner: OwnerToken,
        path: Path,
        observed_incremental_bytes: int,
        *,
        now: datetime | None = None,
    ) -> int:
        """Record soundly attributed additional bytes for one filesystem.

        Returns the filesystem's new remaining liability.  Shared or uncertain
        attribution is refused on purpose: those keep their full conservative
        bound instead of a number we cannot defend.
        """
        if observed_incremental_bytes < 0:
            raise EstimateUnavailableError("observed growth cannot be negative")
        moment = _utcnow(now)
        identity = self._probe(path)

        def action(conn: sqlite3.Connection) -> int:
            existing = self._load_by_key(conn, key)
            if existing is None or existing.state is not ReservationState.RESERVED:
                raise FencingError(f"no live reservation for {key.render()}")
            self._authorise_owner(conn, existing, owner, moment, takeover=False)
            sample = next(
                (item for item in existing.filesystems if item.fs_key == identity.key), None
            )
            if sample is None:
                raise EstimateUnavailableError(
                    f"filesystem {identity.key} is not part of {key.render()}"
                )
            if sample.attribution is Attribution.SHARED_UNCERTAIN:
                raise AttributionUnavailableError(
                    f"{key.render()} shares {sample.fs_key}; its bound stays conservative"
                )
            total = max(sample.observed_bytes, observed_incremental_bytes)
            conn.execute(
                "UPDATE reservation_filesystems SET observed_bytes = ?"
                " WHERE res_id = ? AND fs_key = ?",
                (total, existing.reservation_id, sample.fs_key),
            )
            self._event(
                conn, existing.reservation_id, "OBSERVED", owner.generation, str(total)
            )
            return max(0, sample.bound_bytes - total)

        return self._transaction(action)

    def settle(
        self,
        key: ReservationKey,
        owner: OwnerToken,
        *,
        detail: str = "",
        now: datetime | None = None,
    ) -> StorageDecision:
        """Close a reservation after a verified terminal state (idempotent)."""
        return self._close(key, owner, ReservationState.SETTLED, "SETTLED", detail, now)

    def release(
        self,
        key: ReservationKey,
        owner: OwnerToken,
        *,
        detail: str = "",
        now: datetime | None = None,
    ) -> StorageDecision:
        """Close a reservation whose owned artifacts are accounted for."""
        return self._close(key, owner, ReservationState.RELEASED, "RELEASED", detail, now)

    def _close(
        self,
        key: ReservationKey,
        owner: OwnerToken,
        state: ReservationState,
        event: str,
        detail: str,
        now: datetime | None,
    ) -> StorageDecision:
        moment = _utcnow(now)

        def action(conn: sqlite3.Connection) -> StorageDecision:
            existing = self._load_by_key(conn, key)
            if existing is None:
                raise FencingError(f"unknown reservation {key.render()}")
            if owner.owner_id != existing.owner_id or owner.generation != existing.owner_generation:
                raise FencingError(
                    f"owner mismatch closing {key.render()}: generation {owner.generation}"
                )
            if existing.state is state:
                return StorageDecision(
                    outcome=AdmissionOutcome.ADMITTED,
                    reason_code=REASON_OK,
                    reason=f"already {state}",
                    filesystems=(),
                    estimate_version=existing.estimate_version,
                    confidence=existing.confidence,
                    margin_authorised=True,
                    margin_source="ledger",
                    reserved=False,
                )
            if existing.state is not ReservationState.RESERVED:
                raise FencingError(
                    f"{key.render()} is already {existing.state}; refusing to reopen it"
                )
            column = "settled_at" if state is ReservationState.SETTLED else "released_at"
            conn.execute(
                f"UPDATE reservations SET state = ?, {column} = ?, release_reason = ?"
                " WHERE res_id = ?",
                (state.value, _iso(moment), detail, existing.reservation_id),
            )
            self._event(conn, existing.reservation_id, event, owner.generation, detail)
            return StorageDecision(
                outcome=AdmissionOutcome.ADMITTED,
                reason_code=REASON_OK,
                reason=f"{state}",
                filesystems=(),
                estimate_version=existing.estimate_version,
                confidence=existing.confidence,
                margin_authorised=True,
                margin_source="ledger",
                reserved=False,
            )

        try:
            return self._transaction(action)
        except StorageBudgetError as exc:
            return StorageDecision(
                outcome=AdmissionOutcome.UNAVAILABLE,
                reason_code=exc.reason_code,
                reason=str(exc),
                filesystems=(),
                estimate_version=ESTIMATE_VERSION,
                confidence=0.0,
                margin_authorised=False,
                margin_source="unavailable",
                reserved=False,
            )

    def reclaim_stale(self, *, now: datetime | None = None) -> tuple[ReservationKey, ...]:
        """Retire reservations whose owner is proven dead and whose lease lapsed.

        Both proofs are required.  Liveness ``UNKNOWN``, a live owner, an
        unexpired lease or an unparseable identity keep the row charged:
        stranded reservations must not become permanent, but they must not be
        reclaimed early either.
        """
        moment = _utcnow(now)

        def action(conn: sqlite3.Connection) -> tuple[ReservationKey, ...]:
            reclaimed: list[ReservationKey] = []
            for reservation in self._load_active(conn):
                stale, reason = self._staleness(reservation, moment)
                if not stale or reason != "owner_dead_lease_expired":
                    continue
                conn.execute(
                    "UPDATE reservations SET state = ?, released_at = ?, release_reason = ?"
                    " WHERE res_id = ?",
                    (
                        ReservationState.RELEASED.value,
                        _iso(moment),
                        "stale_reclaimed",
                        reservation.reservation_id,
                    ),
                )
                self._event(
                    conn,
                    reservation.reservation_id,
                    "RECLAIMED",
                    reservation.owner_generation,
                    reason,
                )
                reclaimed.append(reservation.key)
            return tuple(reclaimed)

        return self._transaction(action)

    # -- inspection --------------------------------------------------------

    def active_reservations(self) -> tuple[Reservation, ...]:
        """Return every live reservation in deterministic key order."""
        return self._transaction(lambda conn: self._load_active(conn))

    def history(self, key: ReservationKey) -> tuple[ReservationEvent, ...]:
        """Return the append-only audit events of one reservation."""

        def action(conn: sqlite3.Connection) -> tuple[ReservationEvent, ...]:
            out: list[ReservationEvent] = []
            for row in conn.execute(
                "SELECT res_id, at, event, owner_generation, detail FROM reservation_events"
                " WHERE res_id = ? ORDER BY at, rowid",
                (key.reservation_id,),
            ).fetchall():
                out.append(
                    ReservationEvent(
                        reservation_id=str(row["res_id"]),
                        at=str(row["at"]),
                        event=str(row["event"]),
                        owner_generation=int(row["owner_generation"]),
                        detail=str(row["detail"]),
                    )
                )
            return tuple(out)

        return self._transaction(action)
