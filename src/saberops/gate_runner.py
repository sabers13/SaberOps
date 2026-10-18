"""Gate Runner + project-scoped GateScratchPolicy runtime.

This module implements the bounded execution seam for Orchestrator-v2's
authoritative post-integration gate and the per-project gate-scratch policy
that decides where disposable gate/test scratch lives for that project.

Design law (preserved verbatim from gate scratch policy):

* Orch owns the authoritative full gate.  The Gate Runner is *only* an
  Orch-owned bounded execution role, never an implementation, reviewer,
  acceptance, or publication authority.
* The Gate Runner must execute the exact gate command AT MOST ONCE.
* RAM fallback, when allowed, is permitted ONLY during preflight, BEFORE
  the gate command is launched.  Once the process has started, the
  backend is frozen.  A test failure, non-zero exit, gate timeout, dirty
  worktree, or HEAD mismatch MUST NEVER trigger a second authoritative
  gate launch.
* ``RAM_REQUIRED`` blocks gate execution when preflight cannot safely
  succeed; no silent disk fallback.
* Evidence (receipt + durable log reference) is persisted outside any
  RAM scratch, so RAM cleanup never destroys authoritative evidence.

The project-scoped gate-scratch policy is persisted in a small
deterministic JSON document inside the existing project state directory.
The immutable ``project.json`` identity binding is never touched by this
module: gate-scratch preference is mutable project-local policy, not
project identity, and storing it in ``project.json`` would weaken the
accepted identity-binding invariant.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol, TextIO

from saberops.gate_history import (
    GateHistoryEntry,
    GateKind,
    GateProducer,
)
from saberops.gate_history import (
    append_entry as append_gate_history,
)
from saberops.gate_history import (
    history_path as gate_history_path,
)
from saberops.gate_outcome_projection import (
    GateEvidence,
    GateRole,
    ProjectedDecisionKind,
    project_gate_outcome,
)
from saberops.gate_profiles import (
    GateProfile,
    GateProfileError,
    GateProfileName,
    resolve_profile,
)
from saberops.gate_scope import (
    GateProtectionResult,
    GateScopeError,
    RetirementCheck,
    RetirementEntry,
    ScopeCheck,
    check_gate_protection,
    check_test_retirement,
    check_write_scope,
    definition_digest,
    gate_definition_mismatches,
    git_changed_files,
    is_protected_gate_path,
    normalize_path,
)
from saberops.models import GateScratchMode
from saberops.process import ProcessResult, run_process, split_command
from saberops.project import (
    ProjectIdentity,
    ProjectIdentityError,
    bind_project_state_dir,
    compute_project_identity,
)

SCHEMA_VERSION: Final[int] = 1

# Stable on-disk filename inside the project state directory.  Deliberately
# distinct from the immutable identity marker ``project.json``: this file
# holds mutable per-project execution-policy preference.
_POLICY_FILENAME: Final[str] = "gate-runner-policy.json"
_POLICY_TMP_PREFIX: Final[str] = ".gate-runner-policy-"

# Default Linux tmpfs candidate.  Chosen because Linux commonly exposes
# ``/dev/shm`` as a tmpfs-backed POSIX-shared-memory filesystem.  The
# preflight deliberately does NOT assume this is tmpfs, writable, or has
# capacity -- it verifies each property before declaring the backend safe.
_DEFAULT_TMPFS_CANDIDATE: Final[Path] = Path("/dev/shm")

# Deterministic safety floor (bytes) that must remain free on the chosen
# backend AFTER preflight reserves its own scratch directory.  This keeps
# the host from being driven into tmpfs pressure by a single gate launch.
_DEFAULT_SAFETY_FLOOR_BYTES: Final[int] = 16 * 1024 * 1024  # 16 MiB

# Minimum free-space required to satisfy the preflight before the safety
# floor check is even attempted.  Anything below this is refused outright.
_MIN_USABLE_BYTES: Final[int] = 1 * 1024 * 1024  # 1 MiB

# Stable product vocabulary (labels used in the UI / CLI).
GATE_SCRATCH_LABELS: Final[dict[GateScratchMode, str]] = {
    GateScratchMode.DISK: "Disk (default)",
    GateScratchMode.RAM_PREFERRED: "Prefer RAM, fall back to disk",
    GateScratchMode.RAM_REQUIRED: "Require RAM (block if unavailable)",
}


class GateScratchBackend(StrEnum):
    """Backend actually used for one gate execution.

    Receipt identity for the execution-time backend.

    DISK      -- the ordinary durable scratch on the host filesystem.
                 Receipts produced without any RAM preflight carry the
                 DISK placeholder in ``preflight_evidence.backend``.
    RAM       -- verified tmpfs-backed scratch.  The preflight is a
                 single deterministic ELIGIBILITY step (existence,
                 tmpfs kind, writability, capacity); it tears down its
                 own probe directory.  The actual private scratch the
                 gate uses is created by a SECOND deterministic
                 PRE-LAUNCH step on the same verified tmpfs, so a
                 successful preflight does NOT by itself mean a scratch
                 dir already exists.  If that second pre-launch creation
                 is refused, the launch is blocked (NOT_RUN) -- never a
                 post-launch fallback.
    NOT_RUN   -- the gate subprocess did NOT execute.  This is distinct
                 from a non-zero exit / failed ProcessResult.  It is the
                 only correct value for a pre-execution block (e.g. a
                 failed ``RAM_REQUIRED`` preflight or a refused pre-launch
                 scratch creation).  A receipt with
                 ``actual_scratch_backend=NOT_RUN`` MUST also carry
                 ``invocation_count=0`` and a non-empty
                 ``fallback_reason`` describing the pre-execution
                 failure; no second authoritative gate is ever run.
                 Failed RAM preflight evidence uses ``NOT_RUN`` (never
                 ``DISK``): the preflight did not select a backend for
                 execution.
    """

    DISK = "DISK"
    RAM = "RAM"
    NOT_RUN = "NOT_RUN"


class GateScratchPolicyError(ValueError):
    """Fail-closed error for an invalid project gate-scratch policy."""


class GateScratchPreflightError(RuntimeError):
    """Pre-execution infrastructure/policy failure: gate command is blocked.

    Raised by the Gate Runner when ``RAM_REQUIRED`` preflight cannot
    safely satisfy the requirement, or when any other pre-execution
    invariant prevents the gate from being launched.  No subprocess is
    started on this path; the engine that drives acceptance treats the
    raise as an authoritative preflight-block and rolls back any state it
    may have installed.
    """


class GateRunnerInfrastructureError(RuntimeError):
    """Typed failure for Gate Runner evidence / I/O errors after launch.

    Raised when the runner cannot preserve the authoritative gate
    evidence it is responsible for (durable gate-log write failed,
    receipt persistence failed, post-gate SHA resolver raised, etc.).
    The gate subprocess may or may not have executed -- the runner
    surfaces the typed error so the engine treats the run as
    fail-closed regardless of whether a ProcessResult returned.  No
    silent retry is ever performed.
    """


# ---------------------------------------------------------------------------
# Project-local policy persistence
# ---------------------------------------------------------------------------


def project_policy_path(state_dir: Path | str) -> Path:
    """Return the on-disk path of the per-project gate-scratch policy."""
    return Path(state_dir) / _POLICY_FILENAME


def _coerce_mode(value: object, source_label: str) -> GateScratchMode:
    if not isinstance(value, str):
        raise GateScratchPolicyError(
            f"Gate scratch policy at {source_label}: mode must be a string, "
            f"got {type(value).__name__}"
        )
    try:
        return GateScratchMode(value)
    except ValueError as exc:
        known = ", ".join(mode.value for mode in GateScratchMode)
        raise GateScratchPolicyError(
            f"Gate scratch policy at {source_label}: unknown mode {value!r}; "
            f"known modes: {known}"
        ) from exc


def _parse_payload(
    data: object, source_path: Path | None
) -> GateScratchMode:
    source_label = str(source_path) if source_path is not None else "policy document"
    if not isinstance(data, dict):
        raise GateScratchPolicyError(
            f"Gate scratch policy at {source_label}: top-level must be JSON object"
        )
    allowed_keys = {"version", "mode"}
    unknown = set(data.keys()) - allowed_keys
    if unknown:
        raise GateScratchPolicyError(
            f"Gate scratch policy at {source_label}: unknown keys {sorted(unknown)}"
        )
    if "version" not in data:
        raise GateScratchPolicyError(
            f"Gate scratch policy at {source_label}: missing 'version'"
        )
    version = data["version"]
    if type(version) is not int or version != SCHEMA_VERSION:
        raise GateScratchPolicyError(
            f"Gate scratch policy at {source_label}: version must be integer "
            f"{SCHEMA_VERSION}, got {version!r}"
        )
    if "mode" not in data:
        raise GateScratchPolicyError(
            f"Gate scratch policy at {source_label}: missing 'mode'"
        )
    return _coerce_mode(data["mode"], source_label)


def _read_policy_file(path: Path) -> GateScratchMode | None:
    """Read and validate a single project policy document.

    Returns ``None`` only when the file does not exist.  An existing file
    that is unreadable or malformed raises :class:`GateScratchPolicyError`
    so the caller can fail closed rather than silently revert to DISK.
    """
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GateScratchPolicyError(
            f"Gate scratch policy at {path}: cannot read: {exc}"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GateScratchPolicyError(
            f"Gate scratch policy at {path}: invalid JSON: {exc}"
        ) from exc
    return _parse_payload(data, path)


def load_project_gate_scratch_policy(repo_path: Path | str) -> GateScratchMode:
    """Return the persisted gate-scratch mode for the project at ``repo_path``.

    Returns :attr:`GateScratchMode.DISK` when no policy file exists yet.
    A malformed/unreadable file raises :class:`GateScratchPolicyError`
    rather than silently falling back, because corrupt project authority
    is a fail-closed condition.

    ``project.json`` (the immutable identity binding) is NOT consulted --
    it is read only to ensure we are resolving the policy for the correct
    project state directory.
    """
    repo = Path(repo_path).expanduser()
    identity = compute_project_identity(repo)
    state_dir = _ensure_state_dir_for(identity)
    return load_project_gate_scratch_policy_for_state_dir(state_dir)


def load_project_gate_scratch_policy_for_state_dir(
    state_dir: Path | str,
) -> GateScratchMode:
    """Return the persisted gate-scratch mode for ``state_dir`` directly.

    Lets callers that already hold the project state directory (e.g. an
    authoritative read inside the run-time that already loaded the
    database) avoid recomputing project identity.  A missing file is the
    DISK default; a present-but-corrupt file fails closed.
    """
    path = project_policy_path(state_dir)
    loaded = _read_policy_file(path)
    return GateScratchMode.DISK if loaded is None else loaded


def _ensure_state_dir_for(identity: ProjectIdentity) -> Path:
    """Return the state directory for ``identity``, creating it if needed.

    :func:`bind_project_state_dir` is the canonical binding primitive and
    is the only one that may write ``project.json``.  Reusing it here is
    what keeps ``project.json`` immutable.
    """
    try:
        return bind_project_state_dir(identity)
    except ProjectIdentityError as exc:
        raise GateScratchPolicyError(
            f"Cannot resolve project state directory for "
            f"{identity.canonical_path}: {exc}"
        ) from exc


def _atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    """Write ``payload`` to ``path`` deterministically and atomically.

    The temp file lives next to the destination and is fsynced before
    :func:`os.replace` is called, so a crash cannot leave a half-written
    policy on disk.  This mirrors the persistence discipline already
    used by :func:`saberops.authority.set_authority_mode`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    tmp_path: Path | None = None
    fd: int | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=_POLICY_TMP_PREFIX, suffix=".tmp", dir=str(path.parent)
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        fd = None
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


def set_project_gate_scratch_policy(
    repo_path: Path | str, mode: GateScratchMode | str
) -> Path:
    """Validate and atomically persist the project's gate-scratch mode.

    Validates the requested mode before any filesystem work, ensures the
    project state directory exists (creating the immutable
    ``project.json`` identity binding if this is a first write), then
    writes the policy document via temp-file + :func:`os.replace`.  A
    failure leaves the previous authoritative config byte-for-byte intact
    and removes the temporary file.
    """
    resolved = mode if isinstance(mode, GateScratchMode) else _coerce_mode(
        mode, "policy document"
    )
    repo = Path(repo_path).expanduser()
    identity = compute_project_identity(repo)
    state_dir = _ensure_state_dir_for(identity)
    path = project_policy_path(state_dir)
    payload: dict[str, object] = {"version": SCHEMA_VERSION, "mode": resolved.value}
    _atomic_write_json(path, payload)
    return path


# ---------------------------------------------------------------------------
# tmpfs preflight
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateScratchPreflightEvidence:
    """Pre-execution evidence captured by the tmpfs preflight.

    All capacity numbers are bytes.  ``filesystem_kind`` is the value
    reported by ``stat -f`` (``TMPFS``, ``EXT4``, ...) -- empty string
    when the filesystem cannot be classified.  ``backend`` is the
    :class:`GateScratchBackend` the preflight chose (``RAM`` only when
    every invariant below is satisfied); ``reason`` is a short string
    explaining the decision when ``RAM`` was not selected.
    """

    backend: GateScratchBackend
    candidate_path: str
    filesystem_kind: str
    total_bytes: int
    free_bytes: int
    scratch_dir: str
    safety_floor_bytes: int
    min_usable_bytes: int
    reason: str = ""


@dataclass(frozen=True)
class GateScratchPreflightResult:
    """Outcome of one tmpfs preflight.

    On ``RAM_REQUIRED`` preflight failure the caller MUST treat the
    gate as blocked (no disk fallback).  On ``RAM_PREFERRED`` failure the
    caller may choose ``DISK`` and continue.
    """

    eligible: bool
    evidence: GateScratchPreflightEvidence
    failure_reason: str | None = None


def _tmpfs_mount_for(path: Path) -> str:
    """Return the canonical filesystem kind string for ``path``.

    Reads ``/proc/self/mountinfo`` (the Linux kernel's authoritative
    mount table for the current process) and locates the mount point
    that contains ``path``.  Returns an empty string when nothing can be
    determined -- the preflight treats that as a non-tmpfs backend.
    """
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    try:
        resolved = str(path.resolve())
    except OSError:
        return ""
    best: str = ""
    best_len = -1
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # mountinfo format (abridged):
        #   mount_id parent_id major:minor root mount_point options - fstype source super_opts
        parts = line.split()
        if len(parts) < 10:
            continue
        # Find the separator between option fields and the post-option fields
        try:
            sep_index = parts.index("-")
        except ValueError:
            continue
        if sep_index + 2 > len(parts):
            continue
        fstype = parts[sep_index + 2].strip().upper()
        mount_point = parts[4]
        if mount_point == resolved or resolved.startswith(mount_point.rstrip("/") + "/"):
            if len(mount_point) > best_len:
                best = fstype
                best_len = len(mount_point)
    return best


def _verify_is_tmpfs(candidate: Path) -> str:
    """Return the filesystem kind for ``candidate``; empty string on failure.

    Used as a single, isolated check so the preflight's logic stays
    small and easy to reason about: tmpfs is the only filesystem kind
    we accept for RAM scratch.
    """
    kind = _tmpfs_mount_for(candidate)
    if kind == "TMPFS":
        return kind
    return ""


def _free_bytes(path: Path) -> int:
    try:
        usage = shutil.disk_usage(str(path))
    except OSError:
        return 0
    return int(usage.free)


def _verify_capacity(
    candidate: Path, scratch_dir: Path, safety_floor: int, min_usable: int
) -> tuple[bool, str]:
    """Return ``(ok, reason)`` for the capacity invariant.

    The free-space invariant is:

        free_bytes >= max(min_usable, scratch_dir_overhead + safety_floor)

    where ``scratch_dir_overhead`` is the reserved capacity for the
    private scratch directory itself (1 MiB is more than enough).
    """
    free = _free_bytes(candidate)
    if free <= 0:
        return False, f"could not determine free space at {candidate}"
    if free < min_usable:
        return False, (
            f"insufficient free space at {candidate} "
            f"({free} bytes < minimum {min_usable} bytes)"
        )
    reserved = max(min_usable, safety_floor + (1 * 1024 * 1024))
    if free < reserved:
        return False, (
            f"insufficient free space at {candidate} for safety floor "
            f"({free} bytes < reserved {reserved} bytes)"
        )
    return True, ""


def _make_private_scratch(parent: Path) -> Path | None:
    """Create a private scratch dir under ``parent``; return ``None`` on failure."""
    try:
        # mkdtemp yields a directory with mode 0o700 atomically.
        path = Path(tempfile.mkdtemp(prefix="orchestrator-gate-", dir=str(parent)))
    except OSError:
        return None
    try:
        os.chmod(path, 0o700)
    except OSError:
        # Permissions are best-effort hardening; do not refuse on systems
        # where chmod is restricted (e.g. FAT mounts).  The directory
        # already has private mkdtemp defaults, so this is the
        # additional tightening.
        pass
    write_scratch_owner(path)
    return path


#: Owner record written inside every gate scratch directory (GS-1 lifecycle
#: registration).  It lets a later sweep decide from real process liveness and
#: age without guessing from the directory name.
_SCRATCH_OWNER_FILENAME: Final = ".saberops-scratch-owner.json"
_SCRATCH_PREFIX: Final = "orchestrator-gate-"


def write_scratch_owner(
    scratch: Path,
    *,
    repo: str | None = None,
    started_at: str | None = None,
) -> None:
    """Record the owning process incarnation and start time inside a scratch dir.

    The durable :class:`~saberops.ownership.ProcessIdentity` (PID + boot id +
    process start ticks) is stored, not a bare PID, so a later sweep can tell a
    live owner from a reused PID.  Best-effort: a failure never blocks a gate
    run, but a directory without a readable record is never adopted as
    disposable by a sweep.
    """
    from saberops.ownership import ProcessIdentity

    payload: dict[str, object] = {
        "pid": os.getpid(),
        "repo": repo,
        "started_at": started_at or datetime.now(UTC).isoformat(),
    }
    identity = ProcessIdentity.for_self()
    if identity is not None:
        payload["identity"] = identity.to_dict()
    try:
        (scratch / _SCRATCH_OWNER_FILENAME).write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )
    except OSError:
        return


def read_scratch_owner(scratch: Path) -> dict[str, Any] | None:
    """Read a scratch owner record, or ``None`` when absent/unreadable."""
    try:
        raw = (scratch / _SCRATCH_OWNER_FILENAME).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        decoded = json.loads(raw)
    except ValueError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _scratch_owner_liveness(owner: dict[str, Any]) -> str:
    """Classify a recorded scratch owner as ``LIVE`` / ``DEAD`` / ``UNKNOWN``.

    A durable process identity is preferred (PID reuse safe).  A bare PID is
    only trusted to prove *death*: a live PID may be a different process that
    reused the number, so a bare-PID owner is never enough to authorize a sweep
    while the PID is alive.
    """
    from saberops.ownership import Liveness, ProcessIdentity, probe_liveness

    raw_identity = owner.get("identity")
    if isinstance(raw_identity, dict):
        identity = ProcessIdentity.from_dict(raw_identity)
        if identity is not None:
            liveness = probe_liveness(identity)
            if liveness is Liveness.LIVE:
                return "LIVE"
            if liveness is Liveness.DEAD:
                return "DEAD"
            return "UNKNOWN"
    pid = owner.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return "UNKNOWN"
    if _pid_alive(pid):
        return "LIVE"
    return "DEAD"


def sweep_gate_scratch(
    root: Path | str,
    *,
    now: datetime | None = None,
    liveness: Callable[[dict[str, Any]], str] | None = None,
    deadline: float | None = None,
    clock: Callable[[], float] | None = None,
) -> tuple[str, ...]:
    """Delete only *provably dead-owned* gate scratch directories under ``root``.

    Safety contract (ADR-0003 Amendment 1 §2, review F5):

    * a live owner is never swept, regardless of age;
    * an unknown owner (no record, unreadable record, unknown liveness) is never
      swept -- age and directory name are not authority;
    * symlinked entries and entries that resolve outside ``root`` are skipped;
    * the loop is bounded by ``deadline`` (a monotonic second) so one pass can
      never monopolise the scheduler tick.

    A directory is retired only when its recorded owner is provably dead.  Every
    removal is the caller's responsibility to audit.
    """
    del now  # retained for API stability; age is not used as authority
    moment_clock = clock if clock is not None else time.monotonic
    classify = liveness if liveness is not None else _scratch_owner_liveness
    base = Path(root)
    if not base.is_dir():
        return ()
    try:
        base_resolved = base.resolve()
    except OSError:
        return ()
    removed: list[str] = []
    try:
        entries = sorted(base.iterdir(), key=lambda p: p.name)
    except OSError:
        return ()
    for entry in entries:
        if deadline is not None and moment_clock() >= deadline:
            break
        if not entry.name.startswith(_SCRATCH_PREFIX):
            continue
        if entry.is_symlink():
            continue
        try:
            if not entry.is_dir():
                continue
            if not entry.resolve().is_relative_to(base_resolved):
                continue
        except OSError:
            continue
        owner = read_scratch_owner(entry)
        if owner is None:
            continue
        if classify(owner) != "DEAD":
            continue
        try:
            shutil.rmtree(entry, ignore_errors=False)
        except OSError:
            continue
        removed.append(str(entry))
    return tuple(removed)


def disk_floor_ok(
    paths: Sequence[Path | str], minimum_gb: float = 10.0
) -> tuple[bool, str]:
    """Assess the current fixed free-space floor without lowering any guard.

    Delegates to the single storage-maintenance implementation of the existing
    ``max(min_gb, 10 %)`` floor so the engine gate path and the developer gate
    can never disagree about the effective reserve.
    """
    from saberops.storage_maintenance import _nearest_existing, current_disk_floor_bytes

    for path in paths:
        anchor = _nearest_existing(path)
        if anchor is None:
            return False, f"no-anchor:{path}"
        try:
            stat = os.statvfs(str(anchor))
        except OSError:
            return False, f"statvfs-failed:{path}"
        free = stat.f_bavail * stat.f_frsize
        total = stat.f_blocks * stat.f_frsize
        required = current_disk_floor_bytes(total, minimum_gb)
        if free < required:
            return False, f"below-floor:{path}:{free}<{required}"
    return True, ""


class GateRunnerDiskLowError(GateRunnerInfrastructureError):
    """The gate launch was blocked by the current free-space floor (ADR-0003 D4).

    A typed INFRA block: no gate subprocess ran and no scratch was created.
    """

    reason_code = "DISK_LOW"


def default_gate_disk_guard(target_repo: Path, *, minimum_gb: float = 10.0) -> str | None:
    """Return a typed infra reason when the gate must not launch, else ``None``.

    Uses the shared current-floor computation on the target repository's
    filesystem.  Unknown (unreadable) capacity fails closed as ``DISK_UNKNOWN``.
    """
    ok, reason = disk_floor_ok([target_repo], minimum_gb)
    if ok:
        return None
    if reason.startswith("statvfs-failed"):
        return "DISK_UNKNOWN"
    return "DISK_LOW"


def preflight_ram_scratch(
    *,
    candidate: Path | str | None = None,
    safety_floor_bytes: int = _DEFAULT_SAFETY_FLOOR_BYTES,
    min_usable_bytes: int = _MIN_USABLE_BYTES,
) -> GateScratchPreflightResult:
    """Verify the candidate RAM backend can safely host gate scratch.

    Returns :class:`GateScratchPreflightResult` describing the outcome.

    On success the result carries the chosen :class:`GateScratchBackend`
    ``RAM`` with a populated :class:`GateScratchPreflightEvidence`.  On
    failure the result is :class:`GateScratchPreflightResult` with
    ``eligible=False``, ``backend=NOT_RUN`` (the preflight did not
    select a backend for execution -- a failed RAM eligibility check is
    never mislabeled ``DISK``), and a stable ``failure_reason``
    string suitable for a receipt's ``fallback_reason``.

    The verification sequence is:

    1. ``candidate`` exists;
    2. ``candidate`` is a directory on a tmpfs filesystem (NOT merely
       a directory whose name is ``/dev/shm``);
    3. ``candidate`` is writable by the current process;
    4. a private scratch directory can be securely created beneath it;
    5. enough free capacity is available BEFORE subtracting the
       configured safety floor.

    Every step is a check, not a side-effect: the scratch directory
    created during step 4 is unlinked after capacity is measured so
    the preflight leaves no residue when it fails.
    """
    target = Path(candidate) if candidate is not None else _DEFAULT_TMPFS_CANDIDATE
    safety_floor = max(0, int(safety_floor_bytes))
    min_usable = max(0, int(min_usable_bytes))
    candidate_path = str(target)

    # 1. exists
    if not target.exists() or not target.is_dir():
        reason = f"candidate RAM path {candidate_path} does not exist"
        return _preflight_failure(target, candidate_path, "", reason, safety_floor, min_usable)

    # 2. tmpfs
    fstype = _verify_is_tmpfs(target)
    if not fstype:
        reason = (
            f"candidate RAM path {candidate_path} is not a tmpfs-backed "
            "filesystem"
        )
        return _preflight_failure(
            target, candidate_path, "", reason, safety_floor, min_usable
        )

    # 3. writable
    if not os.access(str(target), os.W_OK | os.X_OK):
        reason = f"candidate RAM path {candidate_path} is not writable"
        return _preflight_failure(
            target, candidate_path, fstype, reason, safety_floor, min_usable
        )

    # 4. create private scratch
    scratch = _make_private_scratch(target)
    if scratch is None:
        reason = f"could not create isolated scratch dir under {candidate_path}"
        return _preflight_failure(
            target, candidate_path, fstype, reason, safety_floor, min_usable
        )

    # 5. capacity (with safety floor)
    free_after = _free_bytes(scratch)
    ok, reason = _verify_capacity(scratch, scratch, safety_floor, min_usable)
    evidence = GateScratchPreflightEvidence(
        # A failed eligibility check never claims a backend was selected
        # for execution: NOT_RUN, not DISK.
        backend=GateScratchBackend.NOT_RUN,
        candidate_path=candidate_path,
        filesystem_kind=fstype,
        total_bytes=int(shutil.disk_usage(str(scratch)).total),
        free_bytes=free_after,
        scratch_dir=str(scratch),
        safety_floor_bytes=safety_floor,
        min_usable_bytes=min_usable,
        reason="" if ok else reason,
    )
    # The preflight must NOT leave residue: tear the scratch dir down
    # regardless of the verdict.  Cleanup failure is logged as evidence,
    # never as a reason to reinterpret the verdict.
    cleanup_error: str | None = None
    try:
        shutil.rmtree(scratch, ignore_errors=False)
    except OSError as exc:
        cleanup_error = str(exc)
    if cleanup_error is not None:
        return GateScratchPreflightResult(
            eligible=False,
            evidence=evidence,
            failure_reason=(
                f"scratch directory cleanup failed after preflight at "
                f"{scratch}: {cleanup_error}"
            ),
        )
    if not ok:
        return GateScratchPreflightResult(eligible=False, evidence=evidence, failure_reason=reason)
    return GateScratchPreflightResult(
        eligible=True,
        evidence=GateScratchPreflightEvidence(
            backend=GateScratchBackend.RAM,
            candidate_path=evidence.candidate_path,
            filesystem_kind=evidence.filesystem_kind,
            total_bytes=evidence.total_bytes,
            free_bytes=evidence.free_bytes,
            scratch_dir="",  # RAM scratch is recreated by the runner; not a path identity
            safety_floor_bytes=evidence.safety_floor_bytes,
            min_usable_bytes=evidence.min_usable_bytes,
            reason="",
        ),
    )


def _preflight_failure(
    target: Path,
    candidate_path: str,
    fstype: str,
    reason: str,
    safety_floor: int,
    min_usable: int,
) -> GateScratchPreflightResult:
    total = 0
    free = 0
    if target.exists():
        try:
            usage = shutil.disk_usage(str(target))
            total = int(usage.total)
            free = int(usage.free)
        except OSError:
            pass
    return GateScratchPreflightResult(
        eligible=False,
        evidence=GateScratchPreflightEvidence(
            # A failed RAM eligibility check did NOT select a backend for
            # execution: NOT_RUN is the honest value; DISK would be a
            # mislabel.
            backend=GateScratchBackend.NOT_RUN,
            candidate_path=candidate_path,
            filesystem_kind=fstype,
            total_bytes=total,
            free_bytes=free,
            scratch_dir="",
            safety_floor_bytes=safety_floor,
            min_usable_bytes=min_usable,
            reason=reason,
        ),
        failure_reason=reason,
    )


# ---------------------------------------------------------------------------
# Gate Runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateRunnerRequest:
    """Inputs to one bounded authoritative gate execution.

    The request carries exactly the information the runner needs to
    verify the candidate context, perform preflight, execute the
    command, and produce a receipt -- nothing else.

    ``frozen_gate_scratch_mode`` is the mode FROZEN at run creation
    time; the runner never re-reads mutable project policy for an
    already-created run.  ``evidence_dir`` is the durable (non-RAM)
    directory where the runner must persist the receipt and gate log
    reference; ``evidence_dir`` must be a disk-backed path supplied by
    the caller.
    """

    target_repo: Path
    gate_command: str
    timeout: float
    frozen_gate_scratch_mode: GateScratchMode
    candidate_sha: str
    expected_post_gate_sha: str
    evidence_dir: Path
    log_digest_label: str = "orchestrator-gate"
    safety_floor_bytes: int = _DEFAULT_SAFETY_FLOOR_BYTES


@dataclass(frozen=True)
class GateReceipt:
    """Compact deterministic evidence produced by one Gate Runner execution.

    The receipt distinguishes the four mandated cases:
      * requested ``DISK`` and actual ``DISK``;
      * requested ``RAM_PREFERRED`` and actual ``RAM``;
      * requested ``RAM_PREFERRED`` and actual ``DISK`` (fallback);
      * requested ``RAM_REQUIRED`` and preflight blocked -- carried by
        ``actual_scratch_backend=NOT_RUN`` and ``fallback_reason``
        populated with the pre-execution failure; ``invocation_count=0``.

    ``fallback_reason`` is ``None`` when no fallback occurred.  For
    ``actual_scratch_backend=NOT_RUN`` the ``fallback_reason`` carries the
    pre-execution block reason verbatim -- never because a test failed;
    only because preflight could not safely satisfy the requirement
    before launch.

    The receipt preserves :attr:`ProcessResult.passed` semantics:
    ``launch_failed=True``, ``termination_incomplete=True``, or
    ``evidence_persistence_failed=True`` is sufficient to fail the
    authoritative gate verdict, regardless of ``exit_code``.  These
    fields are populated from the underlying ``ProcessResult`` and the
    persistence outcome so a launch-time OSError / unconfirmed
    termination / unpersisted receipt can never accidentally become a
    PASS.
    """

    requested_scratch_mode: GateScratchMode
    actual_scratch_backend: GateScratchBackend
    fallback_reason: str | None
    scratch_path: str
    candidate_sha: str
    post_gate_candidate_sha: str
    post_gate_worktree_clean: bool
    gate_command: str
    exit_code: int
    start_timestamp: str
    end_timestamp: str
    duration_seconds: float
    log_path: str
    log_digest: str
    stdout_excerpt: str
    stderr_excerpt: str
    timed_out: bool
    # Mirror the deterministic ProcessResult failure semantics.  See
    # ``ProcessResult.passed``: a successful verdict requires
    # ``exit_code == 0`` AND ``not timed_out`` AND ``not launch_failed``
    # AND ``not termination_incomplete``.  The runner populates these
    # from the underlying ``ProcessResult`` so the receipt alone is
    # sufficient to make the acceptance decision.
    launch_failed: bool = False
    termination_incomplete: bool = False
    # True when the durable gate-log / receipt persistence operations
    # failed and evidence is therefore not authoritative.  Acceptance
    # MUST fail closed when this is True regardless of ``exit_code``.
    evidence_persistence_failed: bool = False
    preflight_evidence: GateScratchPreflightEvidence = field(
        default_factory=lambda: GateScratchPreflightEvidence(
            backend=GateScratchBackend.NOT_RUN,
            candidate_path="",
            filesystem_kind="",
            total_bytes=0,
            free_bytes=0,
            scratch_dir="",
            safety_floor_bytes=_DEFAULT_SAFETY_FLOOR_BYTES,
            min_usable_bytes=_MIN_USABLE_BYTES,
        )
    )
    receipt_digest: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Render the receipt as a JSON-compatible mapping for durable storage."""
        data: dict[str, Any] = {
            "requested_scratch_mode": self.requested_scratch_mode.value,
            "actual_scratch_backend": self.actual_scratch_backend.value,
            "fallback_reason": self.fallback_reason,
            "scratch_path": self.scratch_path,
            "candidate_sha": self.candidate_sha,
            "post_gate_candidate_sha": self.post_gate_candidate_sha,
            "post_gate_worktree_clean": self.post_gate_worktree_clean,
            "gate_command": self.gate_command,
            "exit_code": self.exit_code,
            "start_timestamp": self.start_timestamp,
            "end_timestamp": self.end_timestamp,
            "duration_seconds": self.duration_seconds,
            "log_path": self.log_path,
            "log_digest": self.log_digest,
            "stdout_excerpt": self.stdout_excerpt,
            "stderr_excerpt": self.stderr_excerpt,
            "timed_out": self.timed_out,
            "launch_failed": self.launch_failed,
            "termination_incomplete": self.termination_incomplete,
            "evidence_persistence_failed": self.evidence_persistence_failed,
            "preflight_evidence": {
                "backend": self.preflight_evidence.backend.value,
                "candidate_path": self.preflight_evidence.candidate_path,
                "filesystem_kind": self.preflight_evidence.filesystem_kind,
                "total_bytes": self.preflight_evidence.total_bytes,
                "free_bytes": self.preflight_evidence.free_bytes,
                "scratch_dir": self.preflight_evidence.scratch_dir,
                "safety_floor_bytes": self.preflight_evidence.safety_floor_bytes,
                "min_usable_bytes": self.preflight_evidence.min_usable_bytes,
                "reason": self.preflight_evidence.reason,
            },
        }
        return data

    def passed(self) -> bool:
        """Authoritative ``ProcessResult.passed`` equivalent.

        True iff ``exit_code == 0`` AND ``not timed_out`` AND
        ``not launch_failed`` AND ``not termination_incomplete`` AND
        ``not evidence_persistence_failed``.  A pre-execution block
        (NOT_RUN backend) cannot pass, and unverified evidence
        (``evidence_persistence_failed=True``) can never become a PASS
        -- acceptance MUST fail closed when the durable evidence could
        not be persisted, regardless of ``exit_code``.
        """
        return (
            self.exit_code == 0
            and not self.timed_out
            and not self.launch_failed
            and not self.termination_incomplete
            and not self.evidence_persistence_failed
            and self.actual_scratch_backend != GateScratchBackend.NOT_RUN
        )


def _utc_now_iso() -> str:
    """Return a UTC ISO-8601 timestamp with deterministic second precision."""
    return (
        datetime.now(UTC)
        .replace(microsecond=0)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _excerpt(text: str, limit: int = 4096) -> str:
    """Return a bounded head excerpt for durable evidence."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} bytes]"


@dataclass
class GateRunner:
    """Bounded Gate Runner seam.

    The runner executes the exact gate command AT MOST ONCE per request.
    Backend selection (RAM vs DISK) is decided during preflight, BEFORE
    the subprocess is launched; once launched, the backend is frozen.
    """

    invocation_counter: int = 0
    _disabled: bool = field(default=False, init=False, repr=False)

    @classmethod
    def counting(cls) -> GateRunner:
        """Return a runner that exposes its launch count for tests."""
        return cls()

    def _record_invocation(self) -> None:
        self.invocation_counter += 1

    @staticmethod
    def _resolve_backend(
        requested: GateScratchMode,
    ) -> tuple[
        GateScratchMode,
        GateScratchBackend,
        GateScratchPreflightResult | None,
    ]:
        """Map a frozen requested mode to (effective requested, backend, preflight).

        DISK            -> backend DISK, no preflight.
        RAM_PREFERRED   -> preflight; on success RAM; on failure DISK
                          (preflight failure is the SAFE fallback, never
                          a post-launch fallback).
        RAM_REQUIRED    -> preflight; on success RAM; on failure
                          NOT_RUN (engine must treat this as a typed
                          pre-execution block and roll back; invocation
                          count remains zero).
        """
        if requested == GateScratchMode.DISK:
            return requested, GateScratchBackend.DISK, None
        preflight = preflight_ram_scratch()
        if preflight.eligible:
            return requested, GateScratchBackend.RAM, preflight
        if requested == GateScratchMode.RAM_REQUIRED:
            return requested, GateScratchBackend.NOT_RUN, preflight
        # RAM_PREFERRED with ineligible preflight -> disk fallback
        # (still pre-launch; the preflight IS the fallback decision).
        return requested, GateScratchBackend.DISK, preflight

    @staticmethod
    def _prepare_ram_scratch(
        preflight: GateScratchPreflightResult,
    ) -> Path | None:
        """Create the private RAM scratch directory used by the gate.

        This is a deterministic pre-launch step on the same tmpfs the
        preflight already verified: existence + tmpfs + writability +
        capacity.  When the preflight says eligible, this is the only
        safe place for scratch creation -- creating it after backend
        freeze would re-introduce a late-stage race. Returns ``None`` when the
        preflight-verified tmpfs refuses the fresh directory; the
        engine then treats this as a preflight-block and rolls back.
        """
        return _make_private_scratch(_DEFAULT_TMPFS_CANDIDATE)

    def run(
        self,
        request: GateRunnerRequest,
        *,
        post_gate_sha_resolver: Callable[[], tuple[str, bool]] | None = None,
        disk_guard: Callable[[Path], str | None] | None = None,
    ) -> GateReceipt:
        """Execute exactly one gate command, producing a durable receipt.

        ``post_gate_sha_resolver`` is a callable the runner invokes AFTER
        the subprocess returns to read the post-gate candidate SHA and
        worktree-clean state.  The runner never decides acceptance -- it
        only records what happened.  When the resolver is ``None`` the
        receipt records empty post-gate values (still valid for an
        isolated unit test).

        Failure semantics:

        * Pre-execution block (RAM_REQUIRED preflight failure, or
          preflight-verified tmpfs refusing scratch creation):
          ``actual_scratch_backend=NOT_RUN``; ``invocation_count=0``;
          typed ``GateScratchPreflightError`` raised; receipt persists
          ``fallback_reason`` verbatim.
        * Post-launch evidence failure: typed
          ``GateRunnerInfrastructureError`` raised; receipt persists
          ``evidence_persistence_failed=True``; engine MUST roll back.
        * Post-launch resolver failure: typed
          ``GateRunnerInfrastructureError`` raised; engine MUST roll back.

        No path under any branch ever launches a second authoritative
        gate.
        """
        if self._disabled:
            raise RuntimeError("Gate Runner has already executed; reuse is forbidden")
        self._disabled = True

        # GS-1 / ADR-0003 D4: consult the current free-space floor BEFORE any
        # backend choice, scratch creation or subprocess.  A below-floor or
        # unknown reading is a typed INFRA block with zero launches.  The
        # parameter exists so a caller can inject a bound guard; the default is
        # the shared current floor, so production gates are always checked.
        guard = disk_guard if disk_guard is not None else default_gate_disk_guard
        guard_reason = guard(Path(request.target_repo))
        if guard_reason is not None:
            raise GateRunnerDiskLowError(f"gate launch blocked: {guard_reason}")

        target = Path(request.target_repo).expanduser().resolve()
        evidence_dir = Path(request.evidence_dir).expanduser().resolve()
        evidence_dir.mkdir(parents=True, exist_ok=True)

        # Backend choice happens BEFORE the subprocess launch.  Once we
        # have called ``run_process`` below, the backend is frozen.
        requested = request.frozen_gate_scratch_mode
        backend_pre = self._resolve_backend(requested)
        effective_requested, backend, preflight = backend_pre

        # PRE-EXECUTION BLOCK PATH
        # ------------------------
        # Two reasons end up here: RAM_REQUIRED preflight failure OR
        # the preflight-verified tmpfs refusing scratch creation.  In
        # both cases, the gate subprocess must NEVER launch.  The
        # receipt is written, the run never invokes the gate, and a
        # typed error is raised so the engine rolls back.
        if backend == GateScratchBackend.NOT_RUN:
            assert preflight is not None
            receipt, log_path, receipt_path = self._build_blocked_receipt_artifacts(
                request, effective_requested, preflight
            )
            self._persist_blocked_evidence(receipt, log_path, receipt_path)
            raise GateScratchPreflightError(
                f"Gate preflight blocked: {preflight.failure_reason}"
            )

        scratch_dir: Path | None = None
        env_extras: dict[str, str] = {}
        fallback_reason: str | None = None

        if backend == GateScratchBackend.RAM:
            if preflight is not None:
                scratch_dir = self._prepare_ram_scratch(preflight)
            else:
                # RAM backend was selected without an explicit preflight
                # record; synthesize a stub for scratch creation.  In
                # practice this branch is unreachable (RAM backend only
                # comes from a successful preflight) but the type
                # narrowing requires a defensible witness.
                scratch_dir = self._prepare_ram_scratch(
                    GateScratchPreflightResult(
                        eligible=True,
                        evidence=GateScratchPreflightEvidence(
                            backend=GateScratchBackend.RAM,
                            candidate_path=str(_DEFAULT_TMPFS_CANDIDATE),
                            filesystem_kind="TMPFS",
                            total_bytes=0,
                            free_bytes=0,
                            scratch_dir="",
                            safety_floor_bytes=request.safety_floor_bytes,
                            min_usable_bytes=_MIN_USABLE_BYTES,
                        ),
                    )
                )
            if scratch_dir is None:
                # Pre-execution infrastructure failure: tmpfs refused a
                # fresh scratch directory AFTER the preflight said
                # eligible.  Construct a NOT_RUN blocked receipt --
                # ``actual_scratch_backend=NOT_RUN``, invocation_count
                # remains zero, and the typed error is raised so the
                # engine rolls back.  We do NOT silently fall back to
                # disk here: post-launch backend fallback is forbidden.
                assert preflight is not None
                blocked_preflight = GateScratchPreflightResult(
                    eligible=False,
                    evidence=preflight.evidence,
                    failure_reason=(
                        "preflight-verified tmpfs refused a fresh scratch "
                        "directory at launch; aborting before gate execution"
                    ),
                )
                (
                    receipt,
                    log_path,
                    receipt_path,
                ) = self._build_blocked_receipt_artifacts(
                    request, effective_requested, blocked_preflight
                )
                self._persist_blocked_evidence(receipt, log_path, receipt_path)
                raise GateScratchPreflightError(blocked_preflight.failure_reason or "")
            env_extras = {
                "TMPDIR": str(scratch_dir),
                "TMP": str(scratch_dir),
                "TEMP": str(scratch_dir),
            }
        elif preflight is not None and not preflight.eligible:
            # RAM_PREFERRED preflight fallback to DISK -- pre-launch
            # decision; the preflight IS the fallback decision.
            fallback_reason = (
                f"RAM_PREFERRED preflight fallback to DISK: {preflight.failure_reason}"
            )

        # Mutable counters used for the durable evidence below.
        start_iso = _utc_now_iso()
        stdout_text = ""
        stderr_text = ""
        timed_out = False
        exit_code = -1
        launch_failed = False
        termination_incomplete = False
        result: ProcessResult | None = None
        evidence_persistence_failed = False
        log_id = uuid.uuid4().hex
        log_path = evidence_dir / f"{request.log_digest_label}.{log_id}.log"

        try:
            gate_args = split_command(request.gate_command)
            self._record_invocation()
            result = run_process(
                gate_args,
                cwd=target,
                env=env_extras,
                timeout=request.timeout if request.timeout > 0 else None,
            )
            stdout_text = result.stdout
            stderr_text = result.stderr
            timed_out = result.timed_out
            exit_code = int(result.exit_code)
            launch_failed = result.launch_failed
            termination_incomplete = result.termination_incomplete
        finally:
            # Persist the raw gate log to a disk-backed path BEFORE
            # any RAM scratch cleanup, so a crash or post-launch
            # cleanup failure cannot destroy authoritative evidence.
            # Evidence-persistence failure MUST be raised as a typed
            # error so the engine rolls back; we never claim a
            # receipt survived when the write itself failed.
            end_iso = _utc_now_iso()
            log_blob = _build_gate_log_blob(
                gate_command=request.gate_command,
                target=str(target),
                start_iso=start_iso,
                end_iso=end_iso,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
                exit_code=exit_code,
                timed_out=timed_out,
                launch_failed=launch_failed,
                termination_incomplete=termination_incomplete,
                requested_mode=effective_requested,
                actual_backend=backend,
                preflight_evidence=preflight.evidence
                if preflight is not None
                else _default_preflight_evidence(request),
            )
            try:
                log_path.write_text(
                    json.dumps(log_blob, indent=2) + "\n", encoding="utf-8"
                )
            except OSError as exc:
                # Durable gate-log write failed.  Evidence is not
                # authoritative; raise a typed infrastructure error
                # so the engine rolls back exactly.  This is NOT a
                # second-gate retry path.
                evidence_persistence_failed = True
                self._safe_cleanup(scratch_dir, fallback_reason)
                raise GateRunnerInfrastructureError(
                    f"Gate Runner evidence persistence failed "
                    f"(gate-log write at {log_path}): {exc}"
                ) from exc
            try:
                log_digest = _digest_text(log_path.read_text(encoding="utf-8"))
            except OSError as exc:
                evidence_persistence_failed = True
                self._safe_cleanup(scratch_dir, fallback_reason)
                raise GateRunnerInfrastructureError(
                    f"Gate Runner evidence digest failed "
                    f"(gate-log read at {log_path}): {exc}"
                ) from exc
            # Cleanup is evidence, not a reason to reinterpret PASS/FAIL.
            cleanup_err = self._safe_cleanup(scratch_dir, fallback_reason)
            if cleanup_err is not None and fallback_reason is None:
                fallback_reason = (
                    f"RAM scratch cleanup failed: {cleanup_err} "
                    "(recorded as evidence; gate verdict unchanged)"
                )

        post_gate_sha = ""
        post_clean = True
        if post_gate_sha_resolver is not None:
            try:
                post_gate_sha, post_clean = post_gate_sha_resolver()
            except Exception as exc:
                # Post-gate state resolver failure: evidence integrity
                # is uncertain.  Raise typed infrastructure error so
                # the engine rolls back exactly.  No second gate
                # launch is ever performed here.
                self._safe_cleanup(scratch_dir, fallback_reason)
                raise GateRunnerInfrastructureError(
                    f"Gate Runner post-gate resolver failed: {exc}"
                ) from exc

        receipt_path = evidence_dir / f"gate-receipt.{log_id}.json"
        receipt = GateReceipt(
            requested_scratch_mode=effective_requested,
            actual_scratch_backend=backend,
            fallback_reason=fallback_reason,
            scratch_path=str(scratch_dir) if scratch_dir is not None else "",
            candidate_sha=request.candidate_sha,
            post_gate_candidate_sha=post_gate_sha,
            post_gate_worktree_clean=post_clean,
            gate_command=request.gate_command,
            exit_code=exit_code,
            start_timestamp=start_iso,
            end_timestamp=end_iso,
            duration_seconds=_duration_seconds(start_iso, end_iso),
            log_path=str(log_path),
            log_digest=log_digest,
            stdout_excerpt=_excerpt(stdout_text),
            stderr_excerpt=_excerpt(stderr_text),
            timed_out=timed_out,
            launch_failed=launch_failed,
            termination_incomplete=termination_incomplete,
            evidence_persistence_failed=evidence_persistence_failed,
            preflight_evidence=preflight.evidence
            if preflight is not None
            else _default_preflight_evidence(request),
        )
        receipt = _attach_receipt_digest(receipt)
        try:
            _persist_receipt(receipt_path, receipt)
        except OSError as exc:
            # Receipt persistence failure: gate verdict cannot be
            # confirmed even when ProcessResult was clean.  Surface a
            # typed infrastructure error so the engine rolls back.
            raise GateRunnerInfrastructureError(
                f"Gate Runner receipt persistence failed "
                f"(receipt at {receipt_path}): {exc}"
            ) from exc
        return receipt

    @staticmethod
    def _build_blocked_receipt_artifacts(
        request: GateRunnerRequest,
        effective_requested: GateScratchMode,
        preflight: GateScratchPreflightResult,
    ) -> tuple[GateReceipt, Path, Path]:
        """Build the pre-execution blocked receipt + its durable paths.

        Returns the populated ``GateReceipt`` (NOT_RUN backend, zero
        invocation evidence) plus the gate-log and receipt JSON paths
        inside the caller-supplied evidence directory.  Persistence
        itself is the caller's responsibility (so we can re-raise
        infrastructure errors from the right seam).
        """
        log_path = Path(request.evidence_dir) / (
            f"{request.log_digest_label}.blocked.{uuid.uuid4().hex}.json"
        )
        receipt_path = Path(request.evidence_dir) / (
            f"gate-receipt.blocked.{uuid.uuid4().hex}.json"
        )
        now_iso = _utc_now_iso()
        receipt = GateReceipt(
            requested_scratch_mode=effective_requested,
            actual_scratch_backend=GateScratchBackend.NOT_RUN,
            fallback_reason=(
                f"Gate preflight blocked (invocation_count=0): "
                f"{preflight.failure_reason}"
            ),
            scratch_path="",
            candidate_sha=request.candidate_sha,
            post_gate_candidate_sha="",
            post_gate_worktree_clean=True,
            gate_command=request.gate_command,
            exit_code=-1,
            start_timestamp=now_iso,
            end_timestamp=now_iso,
            duration_seconds=0.0,
            log_path=str(log_path),
            log_digest="",
            stdout_excerpt="",
            stderr_excerpt="",
            timed_out=False,
            launch_failed=False,
            termination_incomplete=False,
            evidence_persistence_failed=False,
            preflight_evidence=preflight.evidence,
        )
        return receipt, log_path, receipt_path

    @staticmethod
    def _persist_blocked_evidence(
        receipt: GateReceipt, log_path: Path, receipt_path: Path
    ) -> None:
        """Persist the blocked-receipt evidence durably and consistently.

        Ordering rule for blocked-evidence consistency: the companion
        gate-log artifact is written FIRST, and the receipt -- the
        artifact that claims a complete evidence set -- is written LAST
        with an explicit ``companion_gate_log_written`` marker.  An
        incomplete evidence set can therefore never contain a receipt
        claiming its companion exists: if the log write fails, no
        receipt exists at all; if the receipt write fails, only an
        orphan log remains (which claims nothing).  Either failing
        raises a typed infrastructure error so the engine rolls back;
        the pre-execution block itself MUST still be reported.
        """
        try:
            log_path.write_text(
                json.dumps(
                    {
                        "reason": receipt.fallback_reason or "",
                        "requested_scratch_mode": receipt.requested_scratch_mode.value,
                        "actual_scratch_backend": receipt.actual_scratch_backend.value,
                        "invocation_count": 0,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            raise GateRunnerInfrastructureError(
                f"Gate Runner blocked-gate-log persistence failed "
                f"(gate-log at {log_path}): {exc}"
            ) from exc
        try:
            _persist_receipt(
                receipt_path,
                receipt,
                payload_extra={"companion_gate_log_written": True},
            )
        except OSError as exc:
            raise GateRunnerInfrastructureError(
                f"Gate Runner blocked-receipt persistence failed "
                f"(receipt at {receipt_path}): {exc}"
            ) from exc

    @staticmethod
    def _safe_cleanup(
        scratch_dir: Path | None,
        existing_fallback_reason: str | None,
    ) -> str | None:
        """Best-effort removal of ``scratch_dir``; record the failure.

        Cleanup failure is recorded as evidence (via the caller's
        ``fallback_reason`` mutation) but NEVER reinterprets the gate
        verdict.  Returns the error string when cleanup failed,
        ``None`` on success or when there is nothing to clean.
        """
        if scratch_dir is None:
            return None
        try:
            shutil.rmtree(scratch_dir, ignore_errors=False)
        except OSError as exc:
            return str(exc)
        return None


def _default_preflight_evidence(
    request: GateRunnerRequest,
) -> GateScratchPreflightEvidence:
    """Synthesize an empty preflight evidence record for the DISK path.

    ``GateRunnerRequest`` does not always carry a preflight outcome (the
    DISK path performs no preflight at all).  Receipts and gate logs
    still need a populated evidence block so consumers can render the
    same shape; this helper is the canonical placeholder.
    """
    return GateScratchPreflightEvidence(
        backend=GateScratchBackend.DISK,
        candidate_path="",
        filesystem_kind="",
        total_bytes=0,
        free_bytes=0,
        scratch_dir="",
        safety_floor_bytes=request.safety_floor_bytes,
        min_usable_bytes=_MIN_USABLE_BYTES,
    )


def _build_gate_log_blob(
    *,
    gate_command: str,
    target: str,
    start_iso: str,
    end_iso: str,
    stdout_text: str,
    stderr_text: str,
    exit_code: int,
    timed_out: bool,
    launch_failed: bool,
    termination_incomplete: bool,
    requested_mode: GateScratchMode,
    actual_backend: GateScratchBackend,
    preflight_evidence: GateScratchPreflightEvidence,
) -> dict[str, Any]:
    return {
        "gate_command": gate_command,
        "target": target,
        "start_timestamp": start_iso,
        "end_timestamp": end_iso,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "launch_failed": launch_failed,
        "termination_incomplete": termination_incomplete,
        "requested_scratch_mode": requested_mode.value,
        "actual_scratch_backend": actual_backend.value,
        "preflight": {
            "backend": preflight_evidence.backend.value,
            "candidate_path": preflight_evidence.candidate_path,
            "filesystem_kind": preflight_evidence.filesystem_kind,
            "total_bytes": preflight_evidence.total_bytes,
            "free_bytes": preflight_evidence.free_bytes,
            "safety_floor_bytes": preflight_evidence.safety_floor_bytes,
            "min_usable_bytes": preflight_evidence.min_usable_bytes,
            "reason": preflight_evidence.reason,
        },
        "stdout": stdout_text,
        "stderr": stderr_text,
    }


def _duration_seconds(start_iso: str, end_iso: str) -> float:
    """Best-effort duration computation; returns 0.0 when either side is malformed."""
    try:
        start = datetime.strptime(start_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        end = datetime.strptime(end_iso, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
    except ValueError:
        return 0.0
    return max(0.0, (end - start).total_seconds())


def _attach_receipt_digest(receipt: GateReceipt) -> GateReceipt:
    """Return ``receipt`` with ``receipt_digest`` populated from its serialized body."""
    data = receipt.as_dict()
    body_for_digest = {k: v for k, v in data.items() if k != "receipt_digest"}
    canonical = json.dumps(body_for_digest, sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return GateReceipt(**{**receipt.__dict__, "receipt_digest": digest})


def _persist_receipt(
    path: Path,
    receipt: GateReceipt,
    *,
    payload_extra: Mapping[str, object] | None = None,
) -> None:
    """Write the receipt durably (atomic temp + replace) at ``path``.

    Path MUST be on a durable (non-RAM, non-tmpfs) filesystem; the
    caller is responsible for choosing ``evidence_dir``.  ``run_process``
    and similar helpers always receive a caller-supplied evidence dir.
    """
    payload = receipt.as_dict()
    if payload_extra:
        payload.update(dict(payload_extra))
    payload["receipt_digest"] = receipt.receipt_digest
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp: Path | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=".gate-receipt-", suffix=".tmp", dir=str(path.parent)
        )
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        tmp = None
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Engine-run gates: host-gates slots, disk admission, profiles, history
# ---------------------------------------------------------------------------

#: Gate kinds that take the exclusive heavy slot and drain every light slot.
_HEAVY_GATE_KINDS: Final[frozenset[GateKind]] = frozenset(
    {GateKind.G3, GateKind.MAIN_GATE, GateKind.MANUAL}
)

#: Raw gate words mapped to their developer-gate exit codes (docs/gate.md).
_WORD_EXIT: Final[dict[str, int]] = {
    "PASS": 0,
    "FLAKY_PASS": 0,
    "FAIL": 1,
    "TIMEOUT": 1,
    "INFRA": 3,
}

_GATE_ROLE_BY_KIND: Final[dict[GateKind, GateRole]] = {
    GateKind.G1: GateRole.G1,
    GateKind.G2: GateRole.G2,
    GateKind.G3: GateRole.G3,
    GateKind.MAIN_GATE: GateRole.G3,
    GateKind.MANUAL: GateRole.G3,
}


class GateSlotWaitTimeout(RuntimeError):
    """The host-gates slot could not be acquired within the wait budget."""

    reason_code = "SLOT_WAIT_TIMEOUT"


# ---------------------------------------------------------------------------
# D-0076: dynamic RAM-based capacity (hermetic, zero extra dependencies)
# ---------------------------------------------------------------------------
#
# Slot counts used to be static (``SABEROPS_GATE_SLOTS=1``,
# ``SABEROPS_LANE_SLOTS=2``).  On a large host that throttles parallel
# progress; on a small one it can drive the machine into OOM.  The dynamic
# policy reads live memory from ``/proc/meminfo`` (``MemAvailable`` /
# ``MemTotal``) with an ``os.sysconf`` fallback and derives the slot and worker
# budgets from it.  Explicit environment knobs still win, so an owner can pin a
# value when needed.

GIB: Final[int] = 1024 ** 3

#: Reserved for the OS, editor/UI and interactive shells before any slot is
#: handed out.
SAFETY_FLOOR_GIB: Final[float] = 2.0
#: Memory cost of one light (G1/G2 lane) slot and one heavy (G3/main) slot.
LIGHT_SLOT_GIB: Final[float] = 1.0
HEAVY_SLOT_GIB: Final[float] = 3.0
#: Per worker-session estimate used for pytest worker auto-scaling.
WORKER_SESSION_GIB: Final[float] = 0.6

#: Absolute ceiling on light slots; the heavy gate drains indices
#: ``0..MAX_LIGHT_SLOTS`` so this is the monopoly-invariant bound.
MAX_LIGHT_SLOTS: Final[int] = 8

#: The heavy slot may double only on a large, otherwise-idle machine.
HEAVY_DOUBLE_MIN_TOTAL_GIB: Final[float] = 32.0
HEAVY_DOUBLE_MIN_AVAILABLE_GIB: Final[float] = 8.0
HEAVY_DOUBLE_MIN_CORES: Final[int] = 12

DEFAULT_SLOT_WAIT_SECONDS: Final[float] = 3600.0

#: Pre-admission memory guard knobs.
MEMORY_GUARD_ENV: Final[str] = "SABEROPS_GATE_MEMORY_GUARD"
MIN_AVAILABLE_GB_ENV: Final[str] = "SABEROPS_GATE_MIN_AVAILABLE_GB"

_MEMINFO_PATH: Final[Path] = Path("/proc/meminfo")
_GUARD_DISABLED: Final[frozenset[str]] = frozenset({"0", "off", "no", "false", "none"})


def read_meminfo(path: Path | str = _MEMINFO_PATH) -> dict[str, int]:
    """Parse ``/proc/meminfo`` into ``{field: bytes}``; empty when unreadable.

    ``/proc/meminfo`` reports kB values; each is multiplied by 1024 so every
    number this module exposes is in bytes.
    """
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep:
            continue
        parts = rest.split()
        if not parts:
            continue
        try:
            values[key.strip()] = int(parts[0]) * 1024
        except ValueError:
            continue
    return values


def _sysconf_bytes(pages_key: str) -> int | None:
    """Return ``SC_*_PAGES * SC_PAGE_SIZE`` bytes, or ``None`` on any failure."""
    try:
        pages = int(os.sysconf(pages_key))
        size = int(os.sysconf("SC_PAGE_SIZE"))
    except (ValueError, OSError, AttributeError):
        return None
    if pages <= 0 or size <= 0:
        return None
    return pages * size


def total_memory_bytes() -> int | None:
    """Total physical memory in bytes via ``/proc/meminfo`` then ``sysconf``."""
    info = read_meminfo()
    if "MemTotal" in info:
        return info["MemTotal"]
    return _sysconf_bytes("SC_PHYS_PAGES")


def available_memory_bytes() -> int | None:
    """Available memory in bytes via ``MemAvailable`` then a free-memory fallback."""
    info = read_meminfo()
    if "MemAvailable" in info:
        return info["MemAvailable"]
    if {"MemFree", "Buffers", "Cached"} <= info.keys():
        return info["MemFree"] + info["Buffers"] + info["Cached"]
    return _sysconf_bytes("SC_AVPHYS_PAGES")


def cpu_core_count() -> int:
    """Return the logical CPU count, clamped to at least one."""
    return max(1, os.cpu_count() or 1)


def dynamic_light_slots(
    *,
    available_bytes: int | None = None,
    cpu_cores: int | None = None,
) -> int:
    """``max(1, min(MAX_LIGHT_SLOTS, (MemAvailable - 2GiB) // 1GiB, cores // 2))``."""
    available = available_memory_bytes() if available_bytes is None else available_bytes
    cores = cpu_core_count() if cpu_cores is None else cpu_cores
    if available is None:
        return 1
    usable = available - int(SAFETY_FLOOR_GIB * GIB)
    if usable <= 0:
        return 1
    by_ram = usable // int(LIGHT_SLOT_GIB * GIB)
    by_cpu = cores // 2
    return max(1, min(MAX_LIGHT_SLOTS, by_ram, by_cpu))


def dynamic_heavy_slots(
    *,
    total_bytes: int | None = None,
    available_bytes: int | None = None,
    cpu_cores: int | None = None,
) -> int:
    """Return 1 by default, 2 only on a large, idle, many-core host (D-0076)."""
    total = total_memory_bytes() if total_bytes is None else total_bytes
    available = available_memory_bytes() if available_bytes is None else available_bytes
    cores = cpu_core_count() if cpu_cores is None else cpu_cores
    if total is None or available is None:
        return 1
    if (
        total >= int(HEAVY_DOUBLE_MIN_TOTAL_GIB * GIB)
        and available >= int(HEAVY_DOUBLE_MIN_AVAILABLE_GIB * GIB)
        and cores >= HEAVY_DOUBLE_MIN_CORES
    ):
        return 2
    return 1


def dynamic_worker_count(
    *,
    available_bytes: int | None = None,
    cpu_cores: int | None = None,
) -> int:
    """``max(1, min(cpu_cores, int(MemAvailable // 0.6GiB)))`` (D-0076)."""
    available = available_memory_bytes() if available_bytes is None else available_bytes
    cores = cpu_core_count() if cpu_cores is None else cpu_cores
    if available is None:
        return cores
    by_ram = int(available // int(WORKER_SESSION_GIB * GIB))
    return max(1, min(cores, by_ram))


def memory_guard_enabled() -> bool:
    """False when ``SABEROPS_GATE_MEMORY_GUARD`` explicitly disables the guard."""
    return os.environ.get(MEMORY_GUARD_ENV, "1").strip().lower() not in _GUARD_DISABLED


def heavy_gate_memory_floor_bytes() -> int:
    """Live headroom a heavy gate needs before it may start (2 GiB + 3 GiB).

    ``SABEROPS_GATE_MIN_AVAILABLE_GB`` overrides the floor in GiB.
    """
    floor = int((SAFETY_FLOOR_GIB + HEAVY_SLOT_GIB) * GIB)
    raw = os.environ.get(MIN_AVAILABLE_GB_ENV, "").strip()
    if not raw:
        return floor
    try:
        override = float(raw)
    except ValueError:
        return floor
    return max(0, int(override * GIB))


def wait_for_memory_budget(
    floor_bytes: int,
    *,
    wait: float,
    notify: Callable[[str], None] | None = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> bool:
    """Soft-queue until available RAM clears ``floor_bytes``, bounded by ``wait``.

    Returns ``True`` when the floor is satisfied (or memory cannot be read --
    the guard never turns an unreadable ``/proc/meminfo`` into a failure) and
    ``False`` when the budget elapsed while memory stayed below the floor.  It
    is a queue, never an OOM crash: the caller then proceeds with its normal
    slot wait.
    """
    if floor_bytes <= 0:
        return True
    moment = clock if clock is not None else time.monotonic
    pause = sleep if sleep is not None else time.sleep
    announce = notify if notify is not None else (lambda _message: None)
    deadline = moment() + max(0.0, wait)
    announced = False
    while True:
        available = available_memory_bytes()
        if available is None or available >= floor_bytes:
            return True
        if not announced:
            announce(
                f"waiting for memory budget: available={available} bytes "
                f"floor={floor_bytes} bytes"
            )
            announced = True
        if moment() >= deadline:
            return False
        pause(min(0.5, max(0.0, deadline - moment())))


def purge_stale_slot_metadata(root: Path | str, indices: Sequence[int]) -> tuple[int, ...]:
    """Unlink ``<index>.json`` whose recorded holder PID is provably dead.

    Self-healing (D-0076): a process that died or was interrupted leaves its
    ``<index>.json`` behind, which then produces a misleading "waiting for ..."
    notice naming a dead PID.  Because ``flock`` is released on process death,
    a dead recorded PID always means the metadata is phantom, so it can be
    removed immediately and the phantom display cleared.
    """
    base = Path(root)
    purged: list[int] = []
    for index in indices:
        meta = base / f"{index}.json"
        try:
            raw = meta.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        pid = record.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or _pid_alive(pid):
            continue
        try:
            meta.unlink()
        except OSError:
            continue
        purged.append(index)
    return tuple(purged)


@dataclass(frozen=True)
class SlotCapacity:
    """Resolved heavy/light slot counts and the slot-wait budget."""

    heavy: int
    light: int
    wait: float
    source: str

    @property
    def drain_lock_count(self) -> int:
        """Lock count the heavy gate must hold to drain every light index.

        The heavy gate drains ``0..max(light, MAX_LIGHT_SLOTS)`` so a lane can
        never acquire an index the heavy gate has not drained, whichever light
        count that lane computed.
        """
        return max(self.light, MAX_LIGHT_SLOTS) + 1


def resolve_slot_capacity(env: Mapping[str, str] | None = None) -> SlotCapacity:
    """Resolve slot counts from the environment or the dynamic D-0076 policy.

    ``SABEROPS_GATE_SLOTS`` / ``SABEROPS_LANE_SLOTS`` / ``SABEROPS_GATE_SLOT_WAIT``
    remain authoritative when set; otherwise heavy/light come from the live
    memory policy.  A malformed value fails closed as
    :class:`GateSlotWaitTimeout`, exactly as the previous static parser did.
    """
    environ = os.environ if env is None else env
    source = "dynamic"
    heavy_raw = environ.get("SABEROPS_GATE_SLOTS", "")
    light_raw = environ.get("SABEROPS_LANE_SLOTS", "")
    wait_raw = environ.get("SABEROPS_GATE_SLOT_WAIT", "")
    try:
        wait = float(wait_raw) if wait_raw.strip() else DEFAULT_SLOT_WAIT_SECONDS
    except ValueError as exc:
        raise GateSlotWaitTimeout(f"invalid slot wait configuration: {exc}") from exc
    if wait < 0:
        raise GateSlotWaitTimeout("slot wait must be >= 0")
    if heavy_raw.strip():
        try:
            heavy = int(heavy_raw)
        except ValueError as exc:
            raise GateSlotWaitTimeout(f"invalid heavy slot configuration: {exc}") from exc
        if heavy < 1:
            raise GateSlotWaitTimeout("heavy slot count must be >= 1")
        source = "env"
    else:
        heavy = dynamic_heavy_slots()
    if light_raw.strip():
        try:
            light = int(light_raw)
        except ValueError as exc:
            raise GateSlotWaitTimeout(f"invalid light slot configuration: {exc}") from exc
        if light < 1:
            raise GateSlotWaitTimeout("light slot count must be >= 1")
        # An explicit light count above the documented ceiling would be usable
        # beyond the index range the heavy gate drains, so it is clamped to
        # MAX_LIGHT_SLOTS; the drained and usable bounds can then never
        # disagree (a lane can never use an index the heavy gate skips).
        light = min(light, MAX_LIGHT_SLOTS)
        source = "env"
    else:
        light = dynamic_light_slots()
    return SlotCapacity(heavy=heavy, light=light, wait=wait, source=source)


def _acquire_all(paths: Sequence[Path], count: int, deadline: float) -> list[TextIO]:
    """Acquire ``count`` exclusive flocks from ``paths`` before ``deadline``."""
    handles: list[TextIO] = []
    for path in paths[:count]:
        path.parent.mkdir(parents=True, exist_ok=True)
        handles.append(path.open("a+"))
    try:
        while True:
            acquired: list[TextIO] = []
            for handle in handles:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired.append(handle)
                except BlockingIOError:
                    pass
            if len(acquired) == count:
                return handles
            for handle in acquired:
                fcntl.flock(handle, fcntl.LOCK_UN)
            if time.monotonic() >= deadline:
                raise GateSlotWaitTimeout("host-gates slot wait exceeded")
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    except BaseException:
        for handle in handles:
            handle.close()
        raise


def _release(handles: Sequence[TextIO]) -> None:
    for handle in handles:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def _acquire_one(paths: Sequence[Path], deadline: float) -> list[TextIO]:
    """Acquire exactly one exclusive flock from the ``paths`` pool."""
    handles: list[TextIO] = []
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        handles.append(path.open("a+"))
    try:
        while True:
            for handle in handles:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return [handle]
                except BlockingIOError:
                    pass
            if time.monotonic() >= deadline:
                raise GateSlotWaitTimeout("host-gates slot wait exceeded")
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    except BaseException:
        for handle in handles:
            handle.close()
        raise


class GateSlotPoolProtocol(Protocol):
    """Structural contract for the engine's host-gates slot pool.

    Lets the executor accept the real file-lock pool or an equivalent test
    double without importing a concrete implementation.
    """

    def heavy(self, *, wait: float | None = None) -> AbstractContextManager[None]: ...

    def light(self, *, wait: float | None = None) -> AbstractContextManager[None]: ...


class GateSlotPool:
    """The engine's ``host-gates`` local capacity pool (ADR-0003 D3).

    Uses exactly the heavy/light flock files the developer gate uses, so an
    engine gate and a shell gate never run past each other:

    * heavy (``saberops-gate-slots``, default 1) -- exclusive, held for the
      whole of G3 / main-gate runs, which then drain every light slot;
    * light (``saberops-gate-light-slots``, default 2) -- held by G1/G2 runs,
      which wait for a free heavy slot (observe) before taking one light.
    """

    def __init__(self, common_dir: Path | str) -> None:
        self.common = Path(common_dir)
        self.heavy_root = self.common / "saberops-gate-slots"
        self.light_root = self.common / "saberops-gate-light-slots"

    @contextlib.contextmanager
    def heavy(self, *, wait: float | None = None) -> Any:
        """Hold the exclusive heavy slot and every potential light slot (drain)."""
        capacity = resolve_slot_capacity()
        budget = capacity.wait if wait is None else wait
        if memory_guard_enabled():
            wait_for_memory_budget(
                heavy_gate_memory_floor_bytes(), wait=budget, notify=print
            )
        deadline = time.monotonic() + budget
        heavy_indices = list(range(capacity.heavy))
        light_indices = list(range(capacity.drain_lock_count))
        purge_stale_slot_metadata(self.heavy_root, heavy_indices)
        purge_stale_slot_metadata(self.light_root, light_indices)
        heavy = _acquire_all(
            [self.heavy_root / f"{i}.lock" for i in heavy_indices],
            capacity.heavy,
            deadline,
        )
        try:
            light = _acquire_all(
                [self.light_root / f"{i}.lock" for i in light_indices],
                len(light_indices),
                deadline,
            )
            try:
                # We hold every exclusive lock, so any metadata here is phantom.
                purge_stale_slot_metadata(self.heavy_root, heavy_indices)
                purge_stale_slot_metadata(self.light_root, light_indices)
                yield
            finally:
                _release(light)
        finally:
            _release(heavy)

    @contextlib.contextmanager
    def light(self, *, wait: float | None = None) -> Any:
        """Wait for a free heavy slot, then hold exactly one light slot."""
        capacity = resolve_slot_capacity()
        budget = capacity.wait if wait is None else wait
        deadline = time.monotonic() + budget
        heavy_indices = list(range(capacity.heavy))
        light_indices = list(range(capacity.light))
        # Observe the heavy slot: block until it is free, then release so the
        # order matches the developer gate (heavy before light, never both
        # while waiting) and cannot deadlock against it.  The heavy gate drains
        # every potential light index, so a free heavy slot also clears the
        # light pool once we observe and drop it.
        purge_stale_slot_metadata(self.heavy_root, heavy_indices)
        observed = _acquire_all(
            [self.heavy_root / f"{i}.lock" for i in heavy_indices],
            capacity.heavy,
            deadline,
        )
        _release(observed)
        purge_stale_slot_metadata(self.light_root, light_indices)
        light = _acquire_one(
            [self.light_root / f"{i}.lock" for i in light_indices], deadline
        )
        try:
            # We hold one light lock; any dead metadata in the pool is phantom.
            purge_stale_slot_metadata(self.light_root, light_indices)
            yield
        finally:
            _release(light)


def host_disk_admission(
    target_repo: Path | str,
    *,
    estimate_bytes: int,
    minimum_gb: float = 10.0,
) -> str | None:
    """Return a typed infra reason when the gate must not launch, else ``None``.

    Two layers, both deterministic:

    1. the shared current free-space floor (ADR-0003 D4) is always enforced;
    2. when the storage ledger is available, the gate's declared estimate is
       admitted through ``host-disk`` (Step 008 admission).  An ``EXCEEDED``
       decision blocks; an ``UNAVAILABLE`` ledger stays advisory because the
       floor in (1) already fails closed.
    """
    reason = default_gate_disk_guard(Path(target_repo), minimum_gb=minimum_gb)
    if reason is not None:
        return reason
    if estimate_bytes <= 0:
        return None
    try:
        from saberops.storage_budget import (
            Attribution,
            EstimateComponent,
            EstimateSource,
            LedgerConfig,
            StorageEstimate,
            StorageLedger,
        )
    except ImportError:  # pragma: no cover - defensive
        return None
    repo = Path(target_repo).expanduser()
    if not repo.is_dir():
        return "DISK_UNKNOWN"
    try:
        estimate = StorageEstimate(
            components=(
                EstimateComponent(
                    path=str(repo),
                    remaining_growth_bytes=int(estimate_bytes),
                    label="engine-gate",
                ),
            ),
            source=EstimateSource.CONSERVATIVE_FALLBACK,
            sample_count=0,
            confidence=1.0,
            conservative_fallback=True,
            attribution=Attribution.OWNED,
            reason="engine gate admission",
        )
        with StorageLedger(LedgerConfig()) as ledger:
            decision = ledger.assess(estimate)
    except Exception:
        # An unreadable/absent ledger stays advisory: the floor above is the
        # deterministic guard and must never be silently bypassed.
        return None
    outcome = getattr(decision, "outcome", None)
    if getattr(outcome, "value", str(outcome)) == "EXCEEDED":
        return f"DISK_ADMISSION_EXCEEDED:{getattr(decision, 'reason_code', '')}"
    return None


def _read_test_sources(repo: Path, paths: Sequence[str], *, head: str) -> dict[str, str]:
    """Read ``path -> source`` for the given paths at ``head`` (empty on miss)."""
    sources: dict[str, str] = {}
    for path in paths:
        completed = subprocess.run(
            ["git", "show", f"{head}:{path}"],
            cwd=repo,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0:
            sources[path] = completed.stdout
    return sources


def _read_quarantine(repo: Path) -> frozenset[str]:
    path = repo / "tests/quarantine.txt"
    if not path.exists():
        return frozenset()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    return frozenset(
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )


# ---------------------------------------------------------------------------
# Gate protection: base-trusted gate definitions (GT-1)
# ---------------------------------------------------------------------------


def _git_bytes(repo: Path, args: Sequence[str], *, label: str) -> bytes:
    """Run one read-only git command, failing closed on any error."""
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise GateRunnerInfrastructureError(
            f"cannot {label} in {repo}: {detail or f'git exited {completed.returncode}'}"
        )
    return completed.stdout


def load_base_gate_definitions(repo: Path | str, base: str) -> dict[str, str]:
    """Load every protected gate definition **from the base SHA**.

    The bytes come from the git object store at ``base`` (``git ls-tree`` +
    ``git show``), never from the candidate working tree, so the engine can
    compare "what the gate would have used" with "what the candidate would use"
    without trusting the candidate's copies.

    Any git failure is a typed infrastructure error, so an unreadable base is a
    fail-closed block rather than a silent acceptance.
    """
    root = Path(repo).expanduser()
    listing = _git_bytes(
        root, ["ls-tree", "-r", "--name-only", base], label=f"list gate definitions at {base}"
    ).decode("utf-8", "replace")
    definitions: dict[str, str] = {}
    for line in listing.splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            candidate = normalize_path(raw)
        except GateScopeError:
            continue
        if not is_protected_gate_path(candidate):
            continue
        definitions[candidate] = _git_bytes(
            root,
            ["show", f"{base}:{candidate}"],
            label=f"read {candidate} at {base}",
        ).decode("utf-8", "replace")
    return definitions


def _candidate_worktree_gate_paths(repo: Path) -> tuple[str, ...]:
    """Protected gate definitions with an uncommitted or untracked state.

    ``git status --porcelain -z --untracked-files=all`` respects the ignore
    rules and reports paths without quoting, so an untracked ``conftest.py``
    cannot hide behind the branch diff.
    """
    listing = _git_bytes(
        repo,
        ["status", "--porcelain", "-z", "--untracked-files=all"],
        label="list the candidate working tree",
    ).decode("utf-8", "replace")
    paths: list[str] = []
    for entry in listing.split("\0"):
        if len(entry) < 4 or entry[2] != " ":
            continue
        try:
            candidate = normalize_path(entry[3:])
        except GateScopeError:
            continue
        if is_protected_gate_path(candidate):
            paths.append(candidate)
    return tuple(sorted(set(paths)))


def _read_worktree_definitions(repo: Path, paths: Sequence[str]) -> dict[str, str]:
    """Read the candidate's working-tree bytes for each path that exists."""
    definitions: dict[str, str] = {}
    for path in paths:
        try:
            definitions[path] = (repo / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            # Deleted or unreadable: the base comparison reports it as a
            # mismatch, which is the fail-closed direction.
            continue
    return definitions


def verify_gate_definitions(
    repo: Path | str,
    base: str,
    *,
    changed_paths: Sequence[str] = (),
    touches: Sequence[str] = (),
) -> GateProtectionResult:
    """Verify the candidate's gate definitions against the base SHA (GT-1).

    Two deterministic sources feed one verdict:

    1. the protected-path check over the candidate branch diff
       (:func:`check_gate_protection`), and
    2. a byte comparison against every gate definition loaded from ``base``,
       including uncommitted and untracked working-tree changes.

    A candidate whose definitions match the base byte-for-byte *is* running the
    base definitions, so the gate it executes is the base-trusted gate.  Any
    difference blocks: the gate cannot run under definitions the candidate
    edited, and the block is an owner question, never a self-authorization.
    """
    root = Path(repo).expanduser()
    verdict = check_gate_protection(changed_paths, touches)
    tampered = set(verdict.tampered_paths)
    detected = set(verdict.detected_by)
    base_verified = False
    candidate_definitions: dict[str, str] = {}
    if base:
        base_definitions = load_base_gate_definitions(root, base)
        candidates = set(base_definitions)
        for path in changed_paths:
            try:
                normalized = normalize_path(path)
            except GateScopeError:
                continue
            if is_protected_gate_path(normalized):
                candidates.add(normalized)
        candidates.update(_candidate_worktree_gate_paths(root))
        candidate_definitions = _read_worktree_definitions(root, sorted(candidates))
        mismatched = gate_definition_mismatches(base_definitions, candidate_definitions)
        tampered.update(mismatched)
        base_verified = not mismatched
        if mismatched:
            detected.add("base-content")
    if not tampered:
        return GateProtectionResult(
            is_protected=False,
            base_sha=base,
            base_verified=base_verified,
            detected_by=tuple(sorted(detected)),
        )
    merged = check_gate_protection(sorted(tampered), touches)
    return GateProtectionResult(
        is_protected=True,
        tampered_paths=merged.tampered_paths,
        declared_paths=merged.declared_paths,
        reason_code=merged.reason_code,
        reason=merged.reason,
        owner_question=merged.owner_question,
        base_sha=base,
        base_verified=base_verified,
        detected_by=tuple(sorted(detected)),
        digests=tuple(
            (path, definition_digest(candidate_definitions[path]))
            for path in merged.tampered_paths
            if path in candidate_definitions
        ),
    )


@dataclass(frozen=True)
class EngineGateRequest:
    """Inputs to one engine-run, evidence-producing gate (ADR-0004 D3)."""

    target_repo: Path
    kind: GateKind | str
    evidence_dir: Path
    base: str = ""
    head_sha: str = ""
    requested_profile: GateProfileName | str | None = None
    state_dir: Path | None = None
    touches: tuple[str, ...] = ()
    must_not_touch: tuple[str, ...] = ()
    retirement: tuple[RetirementEntry, ...] = ()
    changed_files: tuple[str, ...] = ()
    base_test_sources: Mapping[str, str] | None = None
    head_test_sources: Mapping[str, str] | None = None
    disk_estimate_bytes: int = 0
    slot_wait: float | None = None
    extra_env: Mapping[str, str] = field(default_factory=dict)

    def resolved_kind(self) -> GateKind:
        try:
            return self.kind if isinstance(self.kind, GateKind) else GateKind(self.kind)
        except ValueError as exc:
            raise GateProfileError(f"unknown gate kind {self.kind!r}") from exc


@dataclass(frozen=True)
class EngineGateResult:
    """Authoritative result of one engine-run gate."""

    kind: str
    producer: str
    profile: str
    outcome: str
    accepted: bool
    reason: str
    head_sha: str
    tests: int
    failed: tuple[str, ...]
    flaky: tuple[str, ...]
    wall_time: float
    history_path: str
    outcome_path: str
    scope: ScopeCheck | None = None
    retirement: RetirementCheck | None = None
    protection: GateProtectionResult | None = None
    findings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind,
            "producer": self.producer,
            "profile": self.profile,
            "outcome": self.outcome,
            "accepted": self.accepted,
            "reason": self.reason,
            "head_sha": self.head_sha,
            "counts": {"tests": self.tests, "failed": len(self.failed), "flaky": len(self.flaky)},
            "failed": list(self.failed),
            "flaky": list(self.flaky),
            "wall_time": self.wall_time,
            "history_path": self.history_path,
            "findings": list(self.findings),
        }
        if self.scope is not None:
            payload["write_scope"] = self.scope.as_dict()
        if self.retirement is not None:
            payload["retirement"] = self.retirement.as_dict()
        if self.protection is not None:
            payload["gate_protection"] = self.protection.as_dict()
        return payload


class EngineGateExecutor:
    """Run one gate profile and record authoritative engine evidence.

    The engine (never a model) owns G1/G2/G3 execution: it takes the
    ``host-gates`` slot, checks write scope, test retirement and gate
    protection, enforces the profile's wall budget, reads the profile's
    ``outcome.json``, projects the decision fail-closed, and appends the
    engine history line.

    Gate protection (GT-1) is checked before any command runs: the candidate's
    gate definitions are loaded from the base SHA and compared with the
    candidate tree, and any difference blocks the gate as an owner question.
    """

    def __init__(
        self,
        *,
        slot_pool: GateSlotPoolProtocol | None = None,
        disk_check: Callable[..., str | None] | None = None,
    ) -> None:
        self._slot_pool = slot_pool
        self._disk_check = disk_check

    def execute(self, request: EngineGateRequest) -> EngineGateResult:
        kind = request.resolved_kind()
        repo = Path(request.target_repo).expanduser().resolve()
        evidence_dir = Path(request.evidence_dir).expanduser().resolve()
        evidence_dir.mkdir(parents=True, exist_ok=True)
        common = _git_common_dir(repo)
        profiles = _load_profiles(request.state_dir)
        profile = resolve_profile(
            kind, requested=request.requested_profile, profiles=profiles
        )
        started = time.monotonic()
        findings: list[str] = []

        # --- deterministic pre-G1 checks (fail closed, no command runs) ----
        scope: ScopeCheck | None = None
        retirement: RetirementCheck | None = None
        protection: GateProtectionResult | None = None
        try:
            changed_paths = self._changed_paths(request, repo)
            if kind in (GateKind.G1, GateKind.G2):
                scope, retirement = self._preflight_scope(request, repo, changed_paths, findings)
        except Exception as exc:  # noqa: BLE001 - evidence must still be written
            return self._record(
                request, kind, profile, common, evidence_dir, started,
                outcome="BLOCKED",
                accepted=False,
                reason=f"SCOPE_PREFLIGHT_ERROR: {exc}",
                tests=0,
                failed=(),
                flaky=(),
                scope=None,
                retirement=None,
                findings=(*findings, str(exc)),
            )
        if changed_paths is not None:
            try:
                protection = verify_gate_definitions(
                    repo,
                    request.base,
                    changed_paths=changed_paths,
                    touches=request.touches,
                )
            except Exception as exc:  # noqa: BLE001 - evidence must still be written
                # An unreadable base is fail-closed: the engine cannot show that
                # the candidate's gate definitions are the base definitions.
                return self._record(
                    request, kind, profile, common, evidence_dir, started,
                    outcome="BLOCKED",
                    accepted=False,
                    reason=f"GATE_PROTECTION_BLOCKED: GATE_BASE_UNVERIFIABLE: {exc}",
                    tests=0,
                    failed=(),
                    flaky=(),
                    scope=scope,
                    retirement=retirement,
                    findings=(*findings, "GATE_BASE_UNVERIFIABLE", str(exc)),
                )
        if protection is not None and not protection.allowed:
            # GT-1 / ADR-0004 D3: the candidate edited the definitions its own
            # gate would load.  No command runs; the change is an owner question.
            gate_block = f"GATE_PROTECTION_BLOCKED: {protection.reason}"
            return self._record(
                request, kind, profile, common, evidence_dir, started,
                outcome="BLOCKED",
                accepted=False,
                reason=gate_block,
                tests=0,
                failed=(),
                flaky=(),
                scope=scope,
                retirement=retirement,
                protection=protection,
                findings=(*findings, protection.reason_code, protection.owner_question),
            )
        blocked = self._scope_block(scope, retirement)
        if blocked is not None:
            return self._record(
                request, kind, profile, common, evidence_dir, started,
                outcome="BLOCKED",
                accepted=False,
                reason=blocked,
                tests=0,
                failed=(),
                flaky=(),
                scope=scope,
                retirement=retirement,
                protection=protection,
                findings=(*findings, blocked),
            )

        # --- host-disk admission (Step 008) --------------------------------
        if self._disk_check is None:
            disk_reason = host_disk_admission(
                repo, estimate_bytes=request.disk_estimate_bytes
            )
        else:
            disk_reason = self._disk_check(
                repo, estimate_bytes=request.disk_estimate_bytes
            )
        if disk_reason is not None:
            return self._record(
                request, kind, profile, common, evidence_dir, started,
                outcome="INFRA",
                accepted=False,
                reason=f"INFRA {disk_reason}",
                tests=0,
                failed=(),
                flaky=(),
                scope=scope,
                retirement=retirement,
                protection=protection,
                findings=(*findings, disk_reason),
            )

        # --- host-gates slot + profile execution ---------------------------
        pool = self._slot_pool or GateSlotPool(common)
        try:
            if kind in _HEAVY_GATE_KINDS:
                with pool.heavy(wait=request.slot_wait):
                    process = self._run_profile(request, kind, profile, repo, evidence_dir)
            else:
                with pool.light(wait=request.slot_wait):
                    process = self._run_profile(request, kind, profile, repo, evidence_dir)
        except GateSlotWaitTimeout as exc:
            return self._record(
                request, kind, profile, common, evidence_dir, started,
                outcome="INFRA",
                accepted=False,
                reason=f"INFRA SLOT_WAIT_TIMEOUT ({exc})",
                tests=0,
                failed=(),
                flaky=(),
                scope=scope,
                retirement=retirement,
                protection=protection,
                findings=(*findings, "SLOT_WAIT_TIMEOUT"),
            )

        # --- read the profile's outcome.json and project the decision ------
        artifact = _newest_outcome_artifact(evidence_dir)
        evidence = _evidence_from_artifact(
            artifact,
            repo=repo,
            kind=kind,
            process=process,
            reason_disk=disk_reason,
        )
        decision = project_gate_outcome(evidence)
        wall = round(time.monotonic() - started, 2)
        outcome_word = (
            "INFRA"
            if decision.decision is ProjectedDecisionKind.INFRA
            else decision.decision.value
        )
        if decision.decision is ProjectedDecisionKind.BLOCKED:
            outcome_word = "BLOCKED"
            findings.append(decision.reason)
        return self._record(
            request, kind, profile, common, evidence_dir, started,
            outcome=outcome_word,
            accepted=decision.accepted,
            reason=decision.reason,
            tests=evidence.tests_ran or 0,
            failed=decision.failed,
            flaky=decision.flaky,
            scope=scope,
            retirement=retirement,
            protection=protection,
            findings=tuple(findings),
            wall=wall,
        )

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _changed_paths(
        request: EngineGateRequest, repo: Path
    ) -> tuple[str, ...] | None:
        """The candidate's changed paths, or ``None`` when no diff is declared.

        ``None`` means the caller declared neither ``changed_files`` nor a
        ``base``, so there is no candidate diff to protect; real G1/G2/G3 runs
        always declare one.
        """
        if request.changed_files:
            return tuple(request.changed_files)
        if request.base:
            return git_changed_files(str(repo), request.base, request.head_sha or "HEAD")
        return None

    def _preflight_scope(
        self,
        request: EngineGateRequest,
        repo: Path,
        changed_paths: tuple[str, ...] | None,
        findings: list[str],
    ) -> tuple[ScopeCheck | None, RetirementCheck | None]:
        scope: ScopeCheck | None = None
        retirement: RetirementCheck | None = None
        if request.touches or request.must_not_touch:
            changed = (
                changed_paths
                if changed_paths is not None
                else git_changed_files(str(repo), request.base, request.head_sha or "HEAD")
            )
            scope = check_write_scope(changed, request.touches, request.must_not_touch)
            findings.extend(scope.as_dict()["out_of_scope"])  # type: ignore[arg-type]
        base_sources = dict(request.base_test_sources or {})
        head_sources = dict(request.head_test_sources or {})
        if request.retirement or base_sources or head_sources:
            retirement = check_test_retirement(base_sources, head_sources, request.retirement)
        return scope, retirement

    @staticmethod
    def _scope_block(scope: ScopeCheck | None, retirement: RetirementCheck | None) -> str | None:
        if scope is not None and not scope.allowed:
            return f"WRITE_SCOPE_VIOLATION: {scope.reason}"
        if retirement is not None and not retirement.allowed:
            return f"TEST_RETIREMENT_VIOLATION: {retirement.reason}"
        return None

    def _run_profile(
        self,
        request: EngineGateRequest,
        kind: GateKind,
        profile: GateProfile,
        repo: Path,
        evidence_dir: Path,
    ) -> ProcessResult | None:
        result_dir = evidence_dir / "result"
        result_dir.mkdir(parents=True, exist_ok=True)
        env = {
            "SABEROPS_GATE_KIND": kind.value,
            "SABEROPS_GATE_PRODUCER": GateProducer.ENGINE.value,
            "SABEROPS_GATE_PROFILE": profile.name.value,
            "SABEROPS_GATE_LABEL": kind.value,
            # The engine already holds the host-gates slot, and it owns the
            # single history line; the child must not re-acquire or duplicate.
            "SABEROPS_GATE_EXTERNAL_SLOT": "1",
            "SABEROPS_GATE_NO_HISTORY": "1",
            "SABEROPS_GATE_RESULT_DIR": str(result_dir),
            **dict(request.extra_env),
        }
        selector_nodes: list[str] = []
        if profile.selector_command:
            selector_nodes = self._run_selector(profile, repo, env)
        deadline = time.monotonic() + profile.wall_budget_seconds
        last: ProcessResult | None = None
        for command in profile.render_commands(
            repo=str(repo),
            base=request.base,
            head=request.head_sha or "HEAD",
            result_dir=str(result_dir),
            python=os.environ.get("SABEROPS_GATE_PYTHON", sys.executable),
            selector_nodes=selector_nodes,
        ):
            if not command:
                continue
            remaining = max(1.0, deadline - time.monotonic())
            last = run_process(command, cwd=repo, env=env, timeout=remaining)
            if last.exit_code != 0 or last.launch_failed or last.timed_out:
                break
        return last

    @staticmethod
    def _run_selector(profile: GateProfile, repo: Path, env: Mapping[str, str]) -> list[str]:
        assert profile.selector_command is not None
        selector = split_command(profile.selector_command)
        if not selector:
            return []
        completed = subprocess.run(
            selector,
            cwd=repo,
            env={**os.environ, **env},
            check=False,
            capture_output=True,
            text=True,
        )
        return [line.strip() for line in completed.stdout.splitlines() if line.strip()]

    def _record(
        self,
        request: EngineGateRequest,
        kind: GateKind,
        profile: GateProfile,
        common: Path,
        evidence_dir: Path,
        started: float,
        *,
        outcome: str,
        accepted: bool,
        reason: str,
        tests: int,
        failed: Sequence[str],
        flaky: Sequence[str],
        scope: ScopeCheck | None,
        retirement: RetirementCheck | None,
        findings: tuple[str, ...],
        protection: GateProtectionResult | None = None,
        wall: float | None = None,
    ) -> EngineGateResult:
        wall = wall if wall is not None else round(time.monotonic() - started, 2)
        head_sha = _resolve_head(request, common)
        ledger = gate_history_path(common)
        outcome_path = evidence_dir / "engine-outcome.json"
        result = EngineGateResult(
            kind=kind.value,
            producer=GateProducer.ENGINE.value,
            profile=profile.name.value,
            outcome=outcome,
            accepted=accepted,
            reason=reason,
            head_sha=head_sha,
            tests=int(tests),
            failed=tuple(sorted(failed)),
            flaky=tuple(sorted(flaky)),
            wall_time=wall,
            history_path=str(ledger),
            outcome_path=str(outcome_path),
            scope=scope,
            retirement=retirement,
            protection=protection,
            findings=findings,
        )
        outcome_path.write_text(json.dumps(result.as_dict(), indent=2) + "\n", encoding="utf-8")
        try:
            append_gate_history(
                ledger,
                GateHistoryEntry(
                    kind=kind,
                    producer=GateProducer.ENGINE.value,
                    head_sha=head_sha,
                    worktree=str(Path(request.target_repo).expanduser()),
                    outcome=outcome,
                    reason=reason,
                    tests=int(tests),
                    failed=tuple(sorted(failed)),
                    flaky=tuple(sorted(flaky)),
                    wall_time=wall,
                    profile=profile.name.value,
                    label=kind.value,
                ),
            )
        except OSError as exc:
            print(f"warning: could not append engine gate history: {exc}", flush=True)
        return result


def _git_common_dir(repo: Path) -> Path:
    completed = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise GateRunnerInfrastructureError(
            f"cannot resolve git common dir for {repo}: {completed.stderr.strip()}"
        )
    raw = completed.stdout.strip()
    return (repo / raw).resolve() if not Path(raw).is_absolute() else Path(raw)


def _load_profiles(state_dir: Path | None) -> Mapping[GateProfileName, GateProfile]:
    from saberops.gate_profiles import default_profiles, load_profiles

    return load_profiles(state_dir) if state_dir is not None else default_profiles()


def _newest_outcome_artifact(evidence_dir: Path) -> dict[str, Any] | None:
    candidates = sorted(
        evidence_dir.rglob("outcome.json"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0.0,
    )
    for path in reversed(candidates):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _evidence_from_artifact(
    artifact: Mapping[str, Any] | None,
    *,
    repo: Path,
    kind: GateKind,
    process: ProcessResult | None,
    reason_disk: str | None,
) -> GateEvidence:
    role = _GATE_ROLE_BY_KIND[kind]
    quarantine = _read_quarantine(repo)
    if artifact is None:
        if process is not None and process.timed_out:
            return GateEvidence(
                raw_outcome="TIMEOUT",
                exit_code=1,
                reason="profile wall budget exceeded",
                has_provenance=True,
                role=role,
            )
        return GateEvidence(
            raw_outcome="INFRA",
            exit_code=3,
            reason=reason_disk or "GATE_ARTIFACT_MISSING",
            has_provenance=True,
            role=role,
        )
    word = str(artifact.get("outcome", ""))
    exit_code = _WORD_EXIT.get(word)
    if process is not None and process.timed_out:
        word, exit_code = "TIMEOUT", 1
    failed = _string_tuple(artifact.get("failed"))
    flaky = _string_tuple(artifact.get("flaky"))
    checks_ok = artifact.get("checks_ok")
    counts = artifact.get("counts")
    tests_ran: int | None = None
    if isinstance(counts, Mapping):
        raw_tests = counts.get("tests")
        if isinstance(raw_tests, int) and not isinstance(raw_tests, bool):
            tests_ran = raw_tests
    selected = artifact.get("selected")
    return GateEvidence(
        raw_outcome=word,
        exit_code=exit_code if exit_code is not None else 3,
        reason=str(artifact.get("reason", "")),
        failed=failed,
        flaky=flaky,
        quarantined=quarantine,
        checks_ok=checks_ok if isinstance(checks_ok, bool) else None,
        selected_nonempty=selected if isinstance(selected, bool) else True,
        tests_ran=tests_ran,
        has_provenance=True,
        producer=GateProducer.ENGINE.value,
        role=role,
    )


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(sorted(str(item) for item in value))


def _resolve_head(request: EngineGateRequest, common: Path) -> str:
    if request.head_sha:
        return request.head_sha
    repo = Path(request.target_repo).expanduser()
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=False, capture_output=True, text=True
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


__all__ = [
    "GATE_SCRATCH_LABELS",
    "GIB",
    "HEAVY_SLOT_GIB",
    "LIGHT_SLOT_GIB",
    "MAX_LIGHT_SLOTS",
    "SAFETY_FLOOR_GIB",
    "EngineGateExecutor",
    "EngineGateRequest",
    "EngineGateResult",
    "GateProtectionResult",
    "GateReceipt",
    "GateRunner",
    "GateRunnerDiskLowError",
    "GateRunnerInfrastructureError",
    "GateRunnerRequest",
    "GateScratchBackend",
    "GateScratchMode",
    "GateScratchPolicyError",
    "GateScratchPreflightError",
    "GateScratchPreflightEvidence",
    "GateScratchPreflightResult",
    "GateSlotPool",
    "GateSlotPoolProtocol",
    "GateSlotWaitTimeout",
    "SlotCapacity",
    "_DEFAULT_SAFETY_FLOOR_BYTES",
    "_DEFAULT_TMPFS_CANDIDATE",
    "_MIN_USABLE_BYTES",
    "available_memory_bytes",
    "cpu_core_count",
    "default_gate_disk_guard",
    "disk_floor_ok",
    "dynamic_heavy_slots",
    "dynamic_light_slots",
    "dynamic_worker_count",
    "heavy_gate_memory_floor_bytes",
    "host_disk_admission",
    "load_base_gate_definitions",
    "load_project_gate_scratch_policy",
    "load_project_gate_scratch_policy_for_state_dir",
    "memory_guard_enabled",
    "preflight_ram_scratch",
    "project_policy_path",
    "purge_stale_slot_metadata",
    "read_meminfo",
    "resolve_slot_capacity",
    "set_project_gate_scratch_policy",
    "sweep_gate_scratch",
    "total_memory_bytes",
    "verify_gate_definitions",
    "wait_for_memory_budget",
]
