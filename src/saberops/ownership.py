"""Durable execution ownership: identity, liveness, and process-group control.

One nonterminal run has at most one authoritative *execution owner*: the
process that is actually driving the orchestration lifecycle.  Ownership is
persisted in the project-local SQLite store (see
:meth:`saberops.db.Database.claim_execution_owner`) and is deliberately
independent of whichever caller *requested* the run.

A raw PID is never trusted on its own.  Every claim persists the non-secret OS
evidence needed to reject PID reuse: the host, the kernel boot id, the process
start time in clock ticks, and the session/process-group ids.  Liveness is a
three-valued answer -- ``LIVE``, ``DEAD``, ``UNKNOWN`` -- and ``UNKNOWN`` always
fails closed for destructive decisions (never relaunch, never kill).
"""

from __future__ import annotations

import json
import os
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

__all__ = [
    "OWNER_ENV_GENERATION",
    "OWNER_ENV_ID",
    "ExecutionOwner",
    "Liveness",
    "OwnerReservation",
    "OwnershipConflictError",
    "OwnershipError",
    "OwnershipStoreError",
    "ProcessIdentity",
    "current_boot_id",
    "parse_owner_generation_env",
    "parse_owner_reservation",
    "probe_liveness",
    "terminate_owner",
    "terminate_process_group",
    "terminate_worker_group_fenced",
    "worker_identity_fences_ok",
]

# Handed to a detached execution owner so it can confirm the reservation made
# on its behalf instead of racing for a fresh claim.
OWNER_ENV_ID = "ORCH_EXECUTION_OWNER_ID"
OWNER_ENV_GENERATION = "ORCH_EXECUTION_OWNER_GENERATION"

_PROC = Path("/proc")


class Liveness(StrEnum):
    """Three-valued process liveness; ``UNKNOWN`` must fail closed."""

    LIVE = "LIVE"
    DEAD = "DEAD"
    UNKNOWN = "UNKNOWN"


class OwnershipError(RuntimeError):
    """Ownership could not be claimed for a structural reason."""


class OwnershipConflictError(OwnershipError):
    """Another owner holds this run and is not provably dead."""


class OwnershipStoreError(OwnershipError):
    """The ownership store could not be read or written.

    This is deliberately distinct from "there is no owner": a locked, corrupt,
    or otherwise unreadable store proves nothing about liveness, so callers must
    treat it as ``UNKNOWN`` and fail closed rather than as evidence of death.
    """


@dataclass(frozen=True)
class OwnerReservation:
    """A launcher's fencing token, handed to the process it detached."""

    owner_id: str
    generation: int


def parse_owner_reservation(env: Mapping[str, str] | None = None) -> OwnerReservation | None:
    """Read a complete reservation token from the environment, or ``None``.

    Fencing needs *both* halves.  A token whose generation is missing,
    malformed, or non-positive yields ``None`` so the holder falls back to an
    ordinary contended claim; it is never downgraded to owner-id-only adoption,
    which would let a stale process adopt a newer reservation.
    """
    source = os.environ if env is None else env
    owner_id = (source.get(OWNER_ENV_ID) or "").strip()
    if not owner_id:
        return None
    raw_generation = (source.get(OWNER_ENV_GENERATION) or "").strip()
    # Strictly the ASCII decimal form this engine writes.  ``int()`` alone also
    # accepts other Unicode digit forms, which is a laxer parse than a fencing
    # token deserves.
    if not raw_generation.isascii() or not raw_generation.isdigit():
        return None
    generation = int(raw_generation)
    if generation <= 0:
        return None
    return OwnerReservation(owner_id=owner_id, generation=generation)


def parse_owner_generation_env(env: Mapping[str, str] | None = None) -> int | None:
    """Read only the generation half of the fencing reservation, or ``None``.

    Useful for callers who need to verify their expected generation matches
    what the parent process handed down (e.g. a recovery child confirming the
    supervisor's reservation, or an orchestrator that already holds the
    matching ``owner_id`` via a direct claim but wants the env-side
    cross-check).  Same strict parse as :func:`parse_owner_reservation`:
    malformed/missing/non-positive generations yield ``None`` rather than 0.
    """
    source = os.environ if env is None else env
    raw_generation = (source.get(OWNER_ENV_GENERATION) or "").strip()
    if not raw_generation.isascii() or not raw_generation.isdigit():
        return None
    generation = int(raw_generation)
    return generation if generation > 0 else None


def current_boot_id() -> str | None:
    """Return this kernel boot's id, or ``None`` where it is not exposed."""
    try:
        return (_PROC / "sys/kernel/random/boot_id").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _read_proc_stat(pid: int) -> list[str] | None:
    """Return ``/proc/<pid>/stat`` fields from field 3 onward, or ``None``.

    ``None`` means the process does not exist.  Parse/permission problems raise
    ``OSError`` so the caller can answer ``UNKNOWN`` rather than ``DEAD``.
    """
    try:
        raw = (_PROC / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except ProcessLookupError:
        return None
    # The comm field is parenthesised and may contain spaces; everything after
    # the final ')' is positional again, starting at field 3 (state).
    close = raw.rfind(")")
    if close < 0:
        raise OSError(f"unparsable /proc/{pid}/stat")
    fields = raw[close + 1 :].split()
    if len(fields) < 20:
        raise OSError(f"truncated /proc/{pid}/stat")
    return fields


@dataclass(frozen=True)
class ProcessIdentity:
    """Non-secret OS evidence identifying one process incarnation."""

    host: str
    boot_id: str | None
    pid: int
    start_ticks: int | None
    session_id: int | None
    process_group_id: int | None

    @classmethod
    def for_pid(cls, pid: int) -> ProcessIdentity | None:
        """Return the identity of a live PID, or ``None`` if it does not exist."""
        fields = _read_proc_stat(pid)
        if fields is None:
            return None
        return cls(
            host=socket.gethostname(),
            boot_id=current_boot_id(),
            pid=int(pid),
            start_ticks=int(fields[19]),
            session_id=int(fields[3]),
            process_group_id=int(fields[2]),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe mapping of this identity."""
        return {
            "host": self.host,
            "boot_id": self.boot_id,
            "pid": self.pid,
            "start_ticks": self.start_ticks,
            "session_id": self.session_id,
            "process_group_id": self.process_group_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProcessIdentity | None:
        """Rebuild an identity from :meth:`to_dict` output, or ``None`` if unusable."""
        try:
            return cls(
                host=str(data["host"]),
                boot_id=None if data.get("boot_id") is None else str(data["boot_id"]),
                pid=int(data["pid"]),
                start_ticks=(None if data.get("start_ticks") is None else int(data["start_ticks"])),
                session_id=None if data.get("session_id") is None else int(data["session_id"]),
                process_group_id=(
                    None if data.get("process_group_id") is None else int(data["process_group_id"])
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None

    @classmethod
    def from_json(cls, raw: str | None) -> ProcessIdentity | None:
        """Rebuild an identity from a JSON string, or ``None`` if unusable."""
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            return cls.from_dict(payload) if isinstance(payload, dict) else None
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    @classmethod
    def for_self(cls) -> ProcessIdentity:
        """Return this process's own identity."""
        identity = cls.for_pid(os.getpid())
        if identity is not None:
            return identity
        # /proc is unavailable: still record what the OS will tell us directly.
        return cls(
            host=socket.gethostname(),
            boot_id=current_boot_id(),
            pid=os.getpid(),
            start_ticks=None,
            session_id=os.getsid(0),
            process_group_id=os.getpgrp(),
        )


def probe_liveness(identity: ProcessIdentity | None) -> Liveness:
    """Decide whether the exact process incarnation in ``identity`` still runs.

    ``DEAD`` is only returned on positive evidence: the PID is gone, the PID has
    been reused by a different incarnation (start time mismatch), the machine
    has rebooted since the claim, or the process has already exited and is
    merely unreaped.  Anything we cannot decide is ``UNKNOWN``.
    """
    if identity is None or identity.pid <= 0:
        return Liveness.UNKNOWN
    if identity.host and identity.host != socket.gethostname():
        # Another machine's process: we can neither confirm nor refute it.
        return Liveness.UNKNOWN
    boot_id = current_boot_id()
    if identity.boot_id and boot_id and identity.boot_id != boot_id:
        # The claim predates a reboot, so that incarnation cannot exist.
        return Liveness.DEAD
    try:
        fields = _read_proc_stat(identity.pid)
    except OSError:
        return Liveness.UNKNOWN
    if fields is None:
        return Liveness.DEAD
    if fields[0] in ("Z", "X", "x"):
        # Exited; still occupying a slot until reaped.
        return Liveness.DEAD
    if identity.start_ticks is None:
        # No reuse discriminator was recorded, so a matching PID proves nothing.
        return Liveness.UNKNOWN
    try:
        observed_start = int(fields[19])
    except ValueError:
        return Liveness.UNKNOWN
    if observed_start != identity.start_ticks:
        # Same PID, different incarnation: the recorded owner is gone.
        return Liveness.DEAD
    return Liveness.LIVE


@dataclass(frozen=True)
class ExecutionOwner:
    """The durable ownership record of one run's execution."""

    run_id: str
    project_id: str | None
    owner_id: str
    generation: int
    state: str
    identity: ProcessIdentity
    claimed_at: str
    heartbeat_at: str | None = None
    released_at: str | None = None
    release_reason: str | None = None
    # Identity of the process that is currently cancelling this owner, set only
    # while ``state`` is ``CANCELLING``.  It exists so an abandoned cancellation
    # (its agent died mid-flight) can be superseded instead of wedging the run.
    cancel_agent: ProcessIdentity | None = None

    CLAIMED = "CLAIMED"
    CANCELLING = "CANCELLING"
    RELEASED = "RELEASED"
    #: States in which the row still asserts ownership of the run.
    ACTIVE_STATES = ("CLAIMED", "CANCELLING")

    @property
    def is_active(self) -> bool:
        """True while this record still asserts ownership.

        ``CANCELLING`` counts as active: a cancellation in flight owns the run
        just as firmly as a running worker, and must block any takeover.
        """
        return self.state in ExecutionOwner.ACTIVE_STATES

    @property
    def is_cancelling(self) -> bool:
        """True while an authoritative cancellation is in progress."""
        return self.state == ExecutionOwner.CANCELLING

    def liveness(self) -> Liveness:
        """Return the liveness of the owning process incarnation."""
        if not self.is_active:
            return Liveness.DEAD
        return probe_liveness(self.identity)
def _group_has_members(pgid: int) -> bool:
    """Return True if the PGID has at least one live (non-zombie) member.

    Delegates to the canonical whole-tree implementation in monitor so there
    is one /proc implementation, not two.
    """
    from saberops.monitor import group_has_live_members

    return group_has_live_members(pgid)


def worker_identity_fences_ok(identity_json: str | None) -> bool:
    """Return True when ``identity_json`` is a valid worker ProcessIdentity that
    matches an alive process with the expected start_ticks on this host."""
    from saberops.db import _decode_process_identity

    if not identity_json:
        return False
    pi = _decode_process_identity(identity_json)
    if pi is None:
        return False
    return probe_liveness(pi) is Liveness.LIVE


def terminate_worker_group_fenced(
    identity_json: str | None,
    *,
    grace_seconds: float = 5.0,
    poll_interval: float = 0.05,
) -> tuple[bool, str]:
    """Fenced worker process-group termination using persisted identity.

    Before signalling, validates that the identity corresponds to a live
    process on this host with matching start_ticks.  Then terminates the
    entire PGID through the one canonical group-extinction primitive
    (:func:`saberops.monitor.terminate_group_confirmed`): SIGTERM,
    bounded grace, SIGKILL if required, confirmed /proc extinction.

    Returns ``(terminated, reason)``.  ``terminated`` is True ONLY when group
    extinction is confirmed (including a group already provably extinct).
    Every other result — identity mismatch, unknown liveness, signal failure,
    incomplete evidence, members remaining — is an unconfirmed destructive
    outcome that callers MUST treat as a refusal to proceed.
    """
    from saberops.db import _decode_process_identity

    if not identity_json:
        return False, "no_worker_identity"

    pi = _decode_process_identity(identity_json)
    if pi is None:
        return False, "unparseable_worker_identity"

    host = socket.gethostname()
    if pi.host and pi.host != host:
        return False, "worker_identity_host_mismatch"

    liveness = probe_liveness(pi)
    if liveness is Liveness.UNKNOWN:
        return False, "worker_liveness_unknown"

    pgid: int | None = None
    if pi.process_group_id is not None and pi.process_group_id > 0:
        pgid = pi.process_group_id

    if liveness is Liveness.DEAD:
        # The leader incarnation is provably gone (start_ticks fence).  It is
        # safe to proceed only when the whole worker group is confirmed
        # extinct; surviving descendants are reported, never guessed at from
        # a dead incarnation's PGID.
        if pgid is not None and pgid == pi.pid:
            from saberops.monitor import snapshot_process_group

            snap = snapshot_process_group(pgid)
            if not snap.has_live_members and not snap.evidence_incomplete:
                return True, "worker_already_extinct"
        return False, "worker_already_dead"

    if pgid is None or pgid != pi.pid:
        return False, "worker_pgid_not_leader"

    from saberops.monitor import terminate_group_confirmed

    ok, reason = terminate_group_confirmed(
        pgid,
        grace_seconds=grace_seconds,
        kill_wait_seconds=5.0,
        poll_interval=poll_interval,
    )
    # Preserve the historical reason vocabulary for this fenced wrapper.
    if reason == "terminated_group":
        reason = "terminated_worker_group"
    elif reason == "killed_group":
        reason = "killed_worker_group"
    elif reason == "already_extinct":
        # Confirmed via a clean /proc snapshot between the liveness probe and
        # the canonical call: the group is provably extinct.
        return True, "worker_already_extinct"
    return ok, reason


def terminate_process_group(
    pid: int,
    *,
    grace_seconds: float = 5.0,
    poll_interval: float = 0.05,
) -> tuple[bool, str]:
    """Terminate an entire process group using canonical whole-tree extinction.

    Uses /proc group-membership scanning (not ``os.kill(pgid, 0)``) so a
    surviving descendant is detected even when the group leader has exited.
    ``pid`` must be the process-group leader (its PID = its PGID).

    Returns ``(success, reason)`` where success is True ONLY when extinction
    is confirmed for all live non-zombie members.
    """
    from saberops.monitor import terminate_group_confirmed

    return terminate_group_confirmed(
        pid,
        grace_seconds=grace_seconds,
        kill_wait_seconds=5.0,
        poll_interval=poll_interval,
    )


def terminate_owner(
    owner: ExecutionOwner,
    *,
    grace_seconds: float = 5.0,
    poll_interval: float = 0.05,
) -> tuple[bool, str]:
    """Terminate an owner's process group, never an unrelated reused PID.

    Returns ``(terminated, reason)``.  The signal target is re-verified against
    the persisted identity immediately before signalling, and a bounded
    ``SIGKILL`` follows only if graceful termination did not take effect.
    """
    liveness = probe_liveness(owner.identity)
    if liveness is Liveness.DEAD:
        return False, "owner_already_dead"
    if liveness is Liveness.UNKNOWN:
        return False, "owner_liveness_unknown"

    # Re-read the live process so the signal cannot land on a PID that was
    # reused between the persisted claim and this instant.
    observed = ProcessIdentity.for_pid(owner.identity.pid)
    if observed is None or observed.start_ticks != owner.identity.start_ticks:
        return False, "owner_already_dead"

    pgid = owner.identity.process_group_id
    use_group = (
        pgid is not None
        and pgid > 0
        and observed.process_group_id == pgid
        and pgid == owner.identity.pid
    )

    def _signal(sig: int) -> bool:
        try:
            if use_group and pgid is not None:
                os.killpg(pgid, sig)
            else:
                os.kill(owner.identity.pid, sig)
        except (OSError, ProcessLookupError):
            return False
        return True

    import signal as _signal_module

    if not _signal(_signal_module.SIGTERM):
        return False, "owner_already_dead"
    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        if probe_liveness(owner.identity) is not Liveness.LIVE:
            return True, "terminated_group" if use_group else "terminated_process"
        time.sleep(poll_interval)
    _signal(_signal_module.SIGKILL)
    return True, "killed_group" if use_group else "killed_process"
