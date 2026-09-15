"""Gate Runner + project-scoped GateScratchPolicy runtime (C13-B).

This module implements the bounded execution seam for Orchestrator-v2's
authoritative post-integration gate and the per-project gate-scratch policy
that decides where disposable gate/test scratch lives for that project.

Design law (preserved verbatim from C13-A):

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

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

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
    return path


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


__all__ = [
    "GATE_SCRATCH_LABELS",
    "GateReceipt",
    "GateRunner",
    "GateRunnerRequest",
    "GateScratchBackend",
    "GateScratchMode",
    "GateScratchPolicyError",
    "GateScratchPreflightError",
    "GateScratchPreflightEvidence",
    "GateScratchPreflightResult",
    "_DEFAULT_SAFETY_FLOOR_BYTES",
    "_DEFAULT_TMPFS_CANDIDATE",
    "_MIN_USABLE_BYTES",
    "load_project_gate_scratch_policy",
    "load_project_gate_scratch_policy_for_state_dir",
    "preflight_ram_scratch",
    "project_policy_path",
    "set_project_gate_scratch_policy",
]
