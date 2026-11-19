#!/usr/bin/env python3
"""Resource ownership, dynamic memory budget, and evidence for gates; no product imports."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

MIB = 1024**2
GB = 1024**3

HISTORY_DIRNAME = "saberops-gate-history"
HISTORY_FILENAME = "full.jsonl"
GATE_LABEL_ENV = "SABEROPS_GATE_LABEL"
DEFAULT_GATE_LABEL = "manual"

LEDGER_DIRNAME = "saberops-memory-ledger"
LEDGER_ENV = "SABEROPS_MEMORY_LEDGER"
DEFAULT_MEMORY_WAIT = 3600.0
MEMORY_WAIT_ENV = "SABEROPS_GATE_SLOT_WAIT"
MEMORY_GUARD_ENV = "SABEROPS_GATE_MEMORY_GUARD"
SABEROPS_GATE_CGROUP_ENV = "SABEROPS_GATE_CGROUP"
EXTERNAL_LEASE_ENV = "SABEROPS_GATE_EXTERNAL_SLOT"
GRANTED_MEMORY_ENV = "SABEROPS_GATE_MEMORY_GRANT"
GRANTED_WORKERS_ENV = "SABEROPS_GRANTED_WORKERS"
PROC_MEMINFO_ENV = "SABEROPS_PROC_MEMINFO"
PROC_PRESSURE_MEMORY_ENV = "SABEROPS_PROC_PRESSURE_MEMORY"
PROC_MOUNTINFO_ENV = "SABEROPS_PROC_MOUNTINFO"

# Calibrated from A2 measured idle baseline (MemTotal 14,908 MiB, Used 6,471 MiB,
# MemAvailable 8,437 MiB) and per-step cgroup memory.peak
# (reports/a2-gate-measurement.md §1, §8; not configuration).
DESKTOP_RESERVE_BYTES = int(2.5 * GB)
SEED_PER_WORKER_PEAK_BYTES = int(180 * MIB)
SEED_STEP_PEAKS_BYTES: dict[str, int] = {
    "affected:small": int(400 * MIB),
    "affected:medium": int(640 * MIB),
    "affected:large": int(871 * MIB),
    "affected:full": int(1101 * MIB),
    "full:full": int(1101 * MIB),
    "mypy:default": int(550 * MIB),
    "coverage-map:full": int(1068 * MIB),
    "configured-gate:default": int(1161 * MIB),
    "accept-gate:default": int(1161 * MIB),
}
COLD_START_PEAK_BYTES = max(SEED_STEP_PEAKS_BYTES.values())

PRESSURE_APPEARS_SOME_AVG10 = 5.0
PRESSURE_APPEARS_FULL_AVG10 = 1.0
PRESSURE_FREEZE_SOME_AVG10 = 15.0
PRESSURE_FREEZE_FULL_AVG10 = 5.0
PRESSURE_CRITICAL_FULL_AVG10 = 10.0
PRESSURE_CLEAR_SOME_AVG10 = 3.0
PRESSURE_CLEAR_FULL_AVG10 = 0.5
DEFAULT_FREEZE_COOLDOWN_SECONDS = 2.0

EWMA_DECAY_ALPHA = 0.4
MAX_STOPPED_RERUNS = 3

_MEMINFO_PATH = Path("/proc/meminfo")
_PRESSURE_PATH = Path("/proc/pressure/memory")
_MOUNTINFO_PATH = Path("/proc/self/mountinfo")
_CGROUP_DISABLED = frozenset({"0", "off", "none", "false", "no"})
_GUARD_DISABLED = frozenset({"0", "off", "no", "false", "none"})
_systemd_run_probed: bool = False
_systemd_run_cache: str | None = None


def read_meminfo(path: Path | None = None) -> dict[str, int]:
    """Parse ``/proc/meminfo`` into ``{field: bytes}``; empty when unreadable."""
    target = path
    if target is None:
        env_path = os.environ.get(PROC_MEMINFO_ENV, "").strip()
        target = Path(env_path) if env_path else _MEMINFO_PATH
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
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
    try:
        pages = int(os.sysconf(pages_key))
        size_bytes = int(os.sysconf("SC_PAGE_SIZE"))
    except (ValueError, OSError, AttributeError):
        return None
    if pages <= 0 or size_bytes <= 0:
        return None
    return pages * size_bytes


def total_memory_bytes(meminfo_path: Path | None = None) -> int | None:
    """Total physical memory in bytes via ``/proc/meminfo`` then ``sysconf``."""
    info = read_meminfo(meminfo_path)
    if "MemTotal" in info:
        return info["MemTotal"]
    if meminfo_path is not None or os.environ.get(PROC_MEMINFO_ENV, "").strip():
        return None
    return _sysconf_bytes("SC_PHYS_PAGES")


def available_memory_bytes(meminfo_path: Path | None = None) -> int | None:
    """Available memory in bytes via ``MemAvailable`` then a free-memory fallback."""
    info = read_meminfo(meminfo_path)
    if "MemAvailable" in info:
        return info["MemAvailable"]
    if {"MemFree", "Buffers", "Cached"} <= set(info):
        return info["MemFree"] + info["Buffers"] + info["Cached"]
    if meminfo_path is not None or os.environ.get(PROC_MEMINFO_ENV, "").strip():
        return None
    return _sysconf_bytes("SC_AVPHYS_PAGES")


def cpu_core_count() -> int:
    return max(1, os.cpu_count() or 1)


def memory_guard_enabled() -> bool:
    return os.environ.get(MEMORY_GUARD_ENV, "1").strip().lower() not in _GUARD_DISABLED


@dataclass(frozen=True)
class MemoryPressure:
    """Parsed ``/proc/pressure/memory`` PSI stall metrics."""

    some_avg10: float = 0.0
    full_avg10: float = 0.0
    some_total: int = 0
    full_total: int = 0


def read_memory_pressure(path: Path | None = None) -> MemoryPressure | None:
    """Parse ``/proc/pressure/memory``; return ``None`` when unavailable/malformed."""
    target = path
    if target is None:
        env_path = os.environ.get(PROC_PRESSURE_MEMORY_ENV, "").strip()
        target = Path(env_path) if env_path else _PRESSURE_PATH
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    some_avg10: float | None = None
    full_avg10: float = 0.0
    some_total: int = 0
    full_total: int = 0
    for raw_line in text.splitlines():
        parts = raw_line.split()
        if not parts:
            continue
        kind = parts[0]
        if kind not in ("some", "full"):
            continue
        metrics: dict[str, str] = {}
        for token in parts[1:]:
            k, sep, v = token.partition("=")
            if sep:
                metrics[k] = v
        try:
            avg10 = float(metrics["avg10"]) if "avg10" in metrics else 0.0
            total = int(metrics["total"]) if "total" in metrics else 0
        except ValueError:
            continue
        if kind == "some":
            some_avg10 = avg10
            some_total = total
        else:
            full_avg10 = avg10
            full_total = total
    if some_avg10 is None:
        return None
    return MemoryPressure(
        some_avg10=some_avg10,
        full_avg10=full_avg10,
        some_total=some_total,
        full_total=full_total,
    )


def _tmpfs_mount_for(path: Path, *, mountinfo_path: Path | None = None) -> str:
    """Return the uppercase filesystem type for ``path`` from ``mountinfo``."""
    target = mountinfo_path
    if target is None:
        env_path = os.environ.get(PROC_MOUNTINFO_ENV, "").strip()
        target = Path(env_path) if env_path else _MOUNTINFO_PATH
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    try:
        resolved = str(path.resolve(strict=False))
    except OSError:
        return ""
    best = ""
    best_len = -1
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # mountinfo: id parent dev root mount_point opts... - fstype source super_opts
        parts = line.split()
        if len(parts) < 9:
            continue
        try:
            sep_index = parts.index("-")
        except ValueError:
            continue
        if sep_index + 1 >= len(parts):
            continue
        fstype = parts[sep_index + 1].strip().upper()
        mount_point = parts[4].replace("\\040", " ")
        normalized_mp = mount_point.rstrip("/") + "/"
        if mount_point == resolved or resolved.startswith(normalized_mp):
            if len(mount_point) > best_len:
                best = fstype
                best_len = len(mount_point)
    return best


def is_tmpfs_path(path: Path, *, mountinfo_path: Path | None = None) -> bool:
    """Return True iff ``path`` resides on a ``tmpfs`` mount."""
    return _tmpfs_mount_for(path, mountinfo_path=mountinfo_path) == "TMPFS"


def disk_scratch_base(
    *,
    mountinfo_path: Path | None = None,
    disk_fallbacks: tuple[Path, ...] | None = None,
) -> Path:
    """Return a writable non-tmpfs base path for gate scratch and snapshots."""
    explicit = os.environ.get("SABEROPS_GATE_TMP", "").strip()
    if explicit:
        target = Path(explicit)
        target.mkdir(parents=True, exist_ok=True)
        return target
    env_tmp = os.environ.get("TMPDIR", "").strip()
    primary = Path(env_tmp) if env_tmp else Path("/tmp")
    if not is_tmpfs_path(primary, mountinfo_path=mountinfo_path):
        return primary
    candidates = disk_fallbacks or (
        Path("/var/tmp"),
        Path.home() / ".cache" / "saberops-tmp",
    )
    for candidate in candidates:
        if is_tmpfs_path(candidate, mountinfo_path=mountinfo_path):
            continue
        try:
            candidate.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        if os.access(candidate, os.W_OK | os.X_OK):
            return candidate
    return Path("/var/tmp")


def gate_scratch_root(*, mountinfo_path: Path | None = None) -> Path:
    """Return the root directory for SaberOps gate scratch runs (never tmpfs by default)."""
    explicit = os.environ.get("SABEROPS_GATE_TMP", "").strip()
    if explicit:
        return Path(explicit)
    user = os.environ.get("USER", str(os.getuid()))
    return disk_scratch_base(mountinfo_path=mountinfo_path) / f"saberops-gate-{user}"


# Evidence labels: kind + producer are part of the history ledger contract
# (ADR-0004 D3).  ``kind`` classifies the lifecycle point; ``producer`` proves
# the gate was run by the engine, never by a model during iteration (G0).
KIND_ENV = "SABEROPS_GATE_KIND"
PRODUCER_ENV = "SABEROPS_GATE_PRODUCER"
PROFILE_ENV = "SABEROPS_GATE_PROFILE"
KNOWN_KINDS = frozenset({"G1", "G2", "G3", "main-gate", "manual"})
DEFAULT_PRODUCER = "developer"
ENGINE_PRODUCER = "engine"


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def positive_int_env(name: str, default: int) -> int:
    """Return a positive integer from ``name``, else the bounded ``default``."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        print(f"warning: {name}={raw!r} is not an integer; using {default}", flush=True)
        return default
    if value < 1:
        print(f"warning: {name}={raw!r} must be >= 1; using {default}", flush=True)
        return default
    return value


def systemd_run_path() -> str | None:
    """Return usable ``systemd-run`` path or ``None`` when disabled/unusable."""
    global _systemd_run_probed, _systemd_run_cache
    raw = os.environ.get(SABEROPS_GATE_CGROUP_ENV)
    if raw is not None and raw.strip().lower() in _CGROUP_DISABLED:
        return None
    if _systemd_run_probed:
        return _systemd_run_cache
    _systemd_run_probed = True
    _systemd_run_cache = None
    runner = shutil.which("systemd-run")
    if runner is None:
        return None
    probe_cmd = [
        runner,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "--",
        "true",
    ]
    try:
        probe = subprocess.run(
            probe_cmd, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if probe.returncode == 0:
        _systemd_run_cache = runner
    return _systemd_run_cache


def build_cgroup_scope_prefix(
    grant_bytes: int,
    *,
    unit_name: str,
    meta_dir: Path,
) -> list[str]:
    """Return ``systemd-run --user --scope`` prefix with per-step ``MemoryHigh``."""
    runner = systemd_run_path()
    if runner is None:
        return []
    mem_high = max(4096, int(grant_bytes))
    wrapper = (
        'unit="$1"; meta="$2"; shift 2; '
        'cg_rel=""; '
        'while IFS=: read -r hid _ctl hpath; do '
        '[ "$hid" = "0" ] && cg_rel="$hpath"; '
        'done < /proc/self/cgroup 2>/dev/null || true; '
        'cg_dir=""; '
        'case "$cg_rel" in *"$unit"*) '
        'cg_dir="/sys/fs/cgroup${cg_rel}"; '
        'printf "%s\\n" "$cg_dir" > "$meta/cgroup_path" ;; '
        'esac; '
        '"$@"; rc=$?; '
        'if [ -n "$cg_dir" ]; then '
        '[ -r "$cg_dir/memory.peak" ] && '
        'cat "$cg_dir/memory.peak" > "$meta/memory.peak" 2>/dev/null || true; '
        '[ -r "$cg_dir/memory.events" ] && '
        'cat "$cg_dir/memory.events" > "$meta/memory.events" 2>/dev/null || true; '
        'fi; '
        'exit "$rc"'
    )
    return [
        runner,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={unit_name}",
        "-p",
        f"MemoryHigh={mem_high}",
        "--",
        "sh",
        "-c",
        wrapper,
        "saberops-cg-wrap",
        unit_name,
        str(meta_dir),
    ]


def _read_cgroup_int(cgroup_dir: Path, filename: str) -> int | None:
    try:
        raw = (cgroup_dir / filename).read_text(encoding="utf-8", errors="replace").strip()
        if not raw or raw == "max":
            return None
        value = int(raw)
        return value if value >= 0 else None
    except (OSError, ValueError):
        return None


def read_cgroup_current(cgroup_dir: Path) -> int | None:
    """Return ``memory.current`` bytes for ``cgroup_dir`` or ``None``."""
    return _read_cgroup_int(cgroup_dir, "memory.current")


def read_cgroup_peak(cgroup_dir: Path) -> int | None:
    """Return ``memory.peak`` bytes for ``cgroup_dir`` or ``None``."""
    return _read_cgroup_int(cgroup_dir, "memory.peak")


def parse_memory_events_oom_kill(text: str) -> int:
    """Extract ``oom_kill`` + ``oom_group_kill`` count from ``memory.events`` text."""
    kills = 0
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] in ("oom_kill", "oom_group_kill"):
            try:
                kills += max(0, int(parts[1]))
            except ValueError:
                pass
    return kills


def read_cgroup_oom_kill(cgroup_dir: Path) -> int:
    """Return OOM kill count from ``cgroup_dir/memory.events``."""
    try:
        text = (cgroup_dir / "memory.events").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return parse_memory_events_oom_kill(text)


def freeze_cgroup(cgroup_dir: Path, unit_name: str = "") -> bool:
    """Freeze a step cgroup via ``cgroup.freeze = 1``; verify unit name when given."""
    if unit_name and unit_name not in str(cgroup_dir):
        return False
    target = cgroup_dir / "cgroup.freeze"
    try:
        target.write_text("1\n", encoding="utf-8")
        return True
    except OSError:
        return False


def thaw_cgroup(cgroup_dir: Path, unit_name: str = "") -> bool:
    """Thaw a step cgroup via ``cgroup.freeze = 0``; verify unit name when given."""
    if unit_name and unit_name not in str(cgroup_dir):
        return False
    target = cgroup_dir / "cgroup.freeze"
    try:
        target.write_text("0\n", encoding="utf-8")
        return True
    except OSError:
        return False


def size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                total += child.stat().st_size
        except FileNotFoundError:
            pass
    return total


def test_directories(base: Path) -> list[Path]:
    directories: list[Path] = []
    for path in base.iterdir():
        if path.is_dir() and not path.is_symlink():
            directories.extend(path.iterdir() if path.name.startswith("popen-gw") else [path])
    return directories


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def owner(path: Path) -> dict[str, Any]:
    try:
        return dict(json.loads((path / "owner.json").read_text()))
    except (OSError, ValueError, TypeError):
        return {}


def remove(path: Path) -> None:
    print(f"remove {path}: {size(path)} bytes", flush=True)
    shutil.rmtree(path)


def sweep(root: Path, legacy: Path | None = None, now: float | None = None) -> None:
    now = time.time() if now is None else now
    for path in root.iterdir():
        if not path.is_dir() or path.is_symlink():
            continue
        record = owner(path)
        # Only owned run directories are ours to remove.
        if record and (not alive(int(record["pid"])) or now - path.stat().st_mtime > 86400):
            remove(path)
    if legacy and legacy.is_dir():
        for path in legacy.glob("pytest-*"):
            if path.is_dir() and not path.is_symlink() and now - path.stat().st_mtime > 86400:
                remove(path)


def required_free(total: int, minimum_gb: float) -> float:
    return max(minimum_gb * GB, total * 0.1)


def disk_ok(paths: list[Path], minimum_gb: float) -> bool:
    okay = True
    for path in paths:
        usage = shutil.disk_usage(path)
        required = required_free(usage.total, minimum_gb)
        print(f"disk {path}: free={usage.free} required={int(required)} bytes", flush=True)
        okay &= usage.free >= required
    return okay


def keep_failed(root: Path, worktree: str, cap: int) -> None:
    failed = [p for p in root.iterdir() if p.is_dir() and owner(p).get("failed")]
    failed.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    seen: set[str] = set()
    kept: list[Path] = []
    for path in failed:
        name = str(owner(path).get("worktree"))
        if name in seen:
            remove(path)
        else:
            seen.add(name)
            kept.append(path)
    total = sum(size(p) for p in kept)
    for path in reversed(kept):
        if total <= cap:
            break
        total -= size(path)
        remove(path)


def selection_size_bucket(
    step: str, selection_count: int | None = None, *, is_full: bool = False
) -> str:
    """Return canonical size bucket key for step peak prediction."""
    if is_full or step in ("full", "coverage-map"):
        return "full"
    if step in ("mypy", "configured-gate", "accept-gate"):
        return "default"
    if selection_count is None:
        return "default"
    if selection_count <= 10:
        return "small"
    if selection_count <= 50:
        return "medium"
    return "large"


def step_key(step: str, size_bucket: str) -> str:
    return f"{step}:{size_bucket}"


def default_ledger_root(common: Path | None = None, *, repo: Path | None = None) -> Path:
    """Return the shared host-wide memory ledger directory beside existing gate slots."""
    explicit = os.environ.get(LEDGER_ENV, "").strip()
    if explicit:
        return Path(explicit)
    if common is not None:
        return Path(common) / LEDGER_DIRNAME
    target = repo or Path.cwd()
    try:
        raw_common = git(target, "rev-parse", "--git-common-dir")
        resolved = Path(raw_common)
        if not resolved.is_absolute():
            resolved = (target / resolved).resolve()
        else:
            resolved = resolved.resolve()
        return resolved / LEDGER_DIRNAME
    except (subprocess.CalledProcessError, OSError):
        return disk_scratch_base() / LEDGER_DIRNAME


def resolve_wait_seconds(wait: float | None = None, env: dict[str, str] | None = None) -> float:
    """Return non-negative memory wait seconds from arg or ``SABEROPS_GATE_SLOT_WAIT``."""
    if wait is not None:
        if wait < 0:
            raise ValueError("memory wait must be >= 0")
        return float(wait)
    environ = os.environ if env is None else env
    raw = environ.get(MEMORY_WAIT_ENV, "").strip()
    if not raw:
        return DEFAULT_MEMORY_WAIT
    parsed = float(raw)
    if parsed < 0:
        raise ValueError("memory wait must be >= 0")
    return parsed


def _ewma_peak(prev: int | None, sample: int) -> int:
    """Asymmetric EWMA: jump upward immediately on spike, decay smoothly downward."""
    if sample <= 0:
        return prev or 0
    if prev is None or prev <= 0:
        return int(sample)
    if sample > prev:
        return int(sample)
    return max(1, round(EWMA_DECAY_ALPHA * sample + (1.0 - EWMA_DECAY_ALPHA) * prev))


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    try:
        raw = path.read_text(encoding="utf-8")
        decoded = json.loads(raw)
    except (OSError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


@dataclass(frozen=True)
class MemoryRunStats:
    """Visibility metrics captured for one budgeted heavy step."""

    step: str
    size_bucket: str
    grant_bytes: int
    measured_peak_bytes: int
    wait_seconds: float
    freeze_count: int
    workers: int = 1
    oom_kill: int = 0
    rerun_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "size_bucket": self.size_bucket,
            "grant_bytes": self.grant_bytes,
            "measured_peak_bytes": self.measured_peak_bytes,
            "wait_seconds": round(self.wait_seconds, 3),
            "freeze_count": self.freeze_count,
            "workers": self.workers,
            "oom_kill": self.oom_kill,
            "rerun_count": self.rerun_count,
        }

    def format_line(self) -> str:
        return (
            f"[memory-budget] step={self.step} bucket={self.size_bucket} "
            f"grant={self.grant_bytes} peak={self.measured_peak_bytes} "
            f"wait={self.wait_seconds:.2f}s freezes={self.freeze_count} "
            f"workers={self.workers}"
            + (f" reruns={self.rerun_count}" if self.rerun_count else "")
        )


class MemoryLease:
    """Active flock-backed memory reservation held until release or process death."""

    def __init__(
        self,
        ledger: MemoryLedger,
        holder_id: str,
        lock_file: IO[str],
        meta_path: Path,
        lock_path: Path,
        *,
        step: str,
        size_bucket: str,
        grant_bytes: int,
        wait_seconds: float,
    ) -> None:
        self._ledger = ledger
        self.holder_id = holder_id
        self._lock_file = lock_file
        self._meta_path = meta_path
        self._lock_path = lock_path
        self.step = step
        self.size_bucket = size_bucket
        self.grant_bytes = int(grant_bytes)
        self.wait_seconds = float(wait_seconds)
        self.cgroup_path: Path | None = None
        self.unit_name: str = ""
        self._released = False

    def __enter__(self) -> MemoryLease:
        return self

    def __exit__(self, *args: object) -> None:
        self.release()

    def set_cgroup_path(self, cgroup_path: Path, *, unit_name: str = "") -> None:
        """Publish this holder's live cgroup directory into the shared ledger."""
        if self._released:
            return
        self.cgroup_path = Path(cgroup_path)
        self.unit_name = unit_name
        self._ledger.update_holder(
            self.holder_id, cgroup_path=str(self.cgroup_path), unit_name=unit_name
        )

    def snapshot_holder(self) -> dict[str, Any]:
        record = _read_json_dict(self._meta_path)
        return record if record is not None else {}

    def is_frozen(self) -> bool:
        return bool(self.snapshot_holder().get("frozen", False))

    def freeze_count(self) -> int:
        raw = self.snapshot_holder().get("freeze_count", 0)
        return int(raw) if isinstance(raw, int) else 0

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._ledger.release_holder(
            self.holder_id, self._lock_file, self._meta_path, self._lock_path
        )


class MemoryLedger:
    """Host-wide flock-protected memory budget, peak learner, and PSI feedback engine."""

    def __init__(
        self,
        root: Path,
        *,
        reserve_bytes: int = DESKTOP_RESERVE_BYTES,
        cold_start_peak_bytes: int = COLD_START_PEAK_BYTES,
        cold_start_per_worker_bytes: int = SEED_PER_WORKER_PEAK_BYTES,
        available_memory_fn: Callable[[], int | None] | None = None,
        pressure_fn: Callable[[], MemoryPressure | None] | None = None,
        cpu_cores_fn: Callable[[], int] | None = None,
        cgroup_current_fn: Callable[[Path], int | None] | None = None,
        freeze_fn: Callable[[Path, str], bool] | None = None,
        thaw_fn: Callable[[Path, str], bool] | None = None,
        clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        freeze_cooldown: float = DEFAULT_FREEZE_COOLDOWN_SECONDS,
    ) -> None:
        self.root = Path(root)
        self.holders_dir = self.root / "holders"
        self.lock_path = self.root / "ledger.lock"
        self.peaks_path = self.root / "peaks.json"
        self.pressure_state_path = self.root / "pressure.json"
        self.reserve_bytes = max(0, int(reserve_bytes))
        self.cold_start_peak_bytes = max(1, int(cold_start_peak_bytes))
        self.cold_start_per_worker_bytes = max(1, int(cold_start_per_worker_bytes))
        self._avail_mem = available_memory_fn or available_memory_bytes
        self._read_pressure = pressure_fn or read_memory_pressure
        self._cpu_cores = cpu_cores_fn or cpu_core_count
        self._cgroup_current = cgroup_current_fn or read_cgroup_current
        self._freeze = freeze_fn or freeze_cgroup
        self._thaw = thaw_fn or thaw_cgroup
        self._clock = clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        self._sleep = sleep or time.sleep
        self._freeze_cooldown = max(0.0, float(freeze_cooldown))

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        self.holders_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _load_peaks_locked(self) -> dict[str, Any]:
        data = _read_json_dict(self.peaks_path)
        if data is None:
            return {"version": 1, "per_worker_bytes": None, "per_worker_samples": 0, "steps": {}}
        steps = data.get("steps")
        if not isinstance(steps, dict):
            data["steps"] = {}
        return data

    def _reap_dead_holders_locked(
        self, exclude_holder_id: str | None = None
    ) -> list[dict[str, Any]]:
        if not self.holders_dir.is_dir():
            return []
        for lock_file in sorted(self.holders_dir.glob("*.lock")):
            hid = lock_file.stem
            if exclude_holder_id is not None and hid == exclude_holder_id:
                continue
            try:
                probe = lock_file.open("a+", encoding="utf-8")
            except OSError:
                continue
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                probe.close()
                continue
            try:
                (self.holders_dir / f"{hid}.json").unlink(missing_ok=True)
                lock_file.unlink(missing_ok=True)
            finally:
                fcntl.flock(probe, fcntl.LOCK_UN)
                probe.close()
        live: list[dict[str, Any]] = []
        for meta_file in sorted(self.holders_dir.glob("*.json")):
            hid = meta_file.stem
            lock_file = self.holders_dir / f"{hid}.lock"
            if not lock_file.exists():
                meta_file.unlink(missing_ok=True)
                continue
            record = _read_json_dict(meta_file)
            if record is not None:
                live.append(record)
        return live

    def _predict_peak_locked(self, step: str, size_bucket: str) -> int:
        data = self._load_peaks_locked()
        steps = data["steps"]
        key = step_key(step, size_bucket)
        entry = steps.get(key)
        if isinstance(entry, dict):
            samples = entry.get("samples", 0)
            peak = entry.get("peak_bytes", 0)
            if isinstance(samples, int) and samples >= 1 and isinstance(peak, int) and peak > 0:
                return peak
        # Before history exists for this (step, size_bucket), assume largest seen so far.
        largest = self.cold_start_peak_bytes
        for item in steps.values():
            if isinstance(item, dict):
                item_peak = item.get("peak_bytes")
                if isinstance(item_peak, int) and item_peak > largest:
                    largest = item_peak
        return largest

    def predict_peak(self, step: str, size_bucket: str = "default") -> int:
        """Return predicted peak bytes for ``(step, size_bucket)``."""
        with self._locked():
            return self._predict_peak_locked(step, size_bucket)

    def predict_per_worker_peak(self, step: str, size_bucket: str = "default") -> int:
        """Return learned per-worker peak bytes, or cold-start per-worker seed."""
        with self._locked():
            data = self._load_peaks_locked()
            entry = data["steps"].get(step_key(step, size_bucket))
            if isinstance(entry, dict):
                val = entry.get("per_worker_bytes")
                if isinstance(val, int) and val > 0:
                    return val
            global_val = data.get("per_worker_bytes")
            if isinstance(global_val, int) and global_val > 0:
                return global_val
            return self.cold_start_per_worker_bytes

    def workers_for_grant(
        self, grant_bytes: int, step: str, size_bucket: str = "default"
    ) -> int:
        """Compute pytest workers as ``clamp(1, cpu_cores, grant // per_worker_peak)``."""
        per_worker = max(1, self.predict_per_worker_peak(step, size_bucket))
        cores = max(1, int(self._cpu_cores()))
        return max(1, min(cores, int(grant_bytes) // per_worker))

    def record_peak(
        self,
        step: str,
        size_bucket: str,
        peak_bytes: int,
        *,
        workers: int | None = None,
    ) -> None:
        """Update learned peak and optional per-worker peak from measured ``memory.peak``."""
        if peak_bytes <= 0:
            return
        with self._locked():
            data = self._load_peaks_locked()
            steps: dict[str, Any] = data["steps"]
            key = step_key(step, size_bucket)
            existing = steps.get(key)
            entry = dict(existing) if isinstance(existing, dict) else {"samples": 0}
            prev_peak = entry.get("peak_bytes")
            prev_peak_int = prev_peak if isinstance(prev_peak, int) else None
            entry["peak_bytes"] = _ewma_peak(prev_peak_int, int(peak_bytes))
            prev_samples = entry.get("samples", 0)
            entry["samples"] = (prev_samples if isinstance(prev_samples, int) else 0) + 1
            if workers is not None and workers >= 1:
                sample_pw = max(1, int(peak_bytes) // int(workers))
                prev_pw = entry.get("per_worker_bytes")
                prev_pw_int = prev_pw if isinstance(prev_pw, int) else None
                entry["per_worker_bytes"] = _ewma_peak(prev_pw_int, sample_pw)
                global_pw = data.get("per_worker_bytes")
                global_pw_int = global_pw if isinstance(global_pw, int) else None
                data["per_worker_bytes"] = _ewma_peak(global_pw_int, sample_pw)
                pw_samples = data.get("per_worker_samples", 0)
                data["per_worker_samples"] = (
                    pw_samples if isinstance(pw_samples, int) else 0
                ) + 1
            entry["updated_at"] = datetime.now(UTC).isoformat()
            steps[key] = entry
            data["steps"] = steps
            _atomic_write_json(self.peaks_path, data)

    def _remaining_budget_locked(
        self, available: int | None, live_holders: list[dict[str, Any]]
    ) -> int | None:
        if available is None:
            return None
        host_budget = max(0, int(available) - self.reserve_bytes)
        reserved_headroom = 0
        for holder in live_holders:
            grant = holder.get("grant_bytes", 0)
            grant_int = int(grant) if isinstance(grant, (int, float)) else 0
            cg_raw = holder.get("cgroup_path", "")
            curr = 0
            if isinstance(cg_raw, str) and cg_raw:
                read_curr = self._cgroup_current(Path(cg_raw))
                if read_curr is not None and read_curr > 0:
                    curr = read_curr
            reserved_headroom += max(0, grant_int - curr)
        return max(0, host_budget - reserved_headroom)

    def remaining_budget(self) -> int | None:
        """Return currently grantable budget in bytes after reserve and live holders."""
        avail = self._avail_mem()
        with self._locked():
            live = self._reap_dead_holders_locked()
            return self._remaining_budget_locked(avail, live)

    def active_holders(self) -> list[dict[str, Any]]:
        """Reap dead holders and return live reservation records."""
        with self._locked():
            return self._reap_dead_holders_locked()

    def _evaluate_pressure_locked(
        self, now: float, live_holders: list[dict[str, Any]]
    ) -> dict[str, Any]:
        pressure = self._read_pressure()
        state = _read_json_dict(self.pressure_state_path) or {}
        was_active = bool(state.get("pressure_active", False))
        prev_some = float(state.get("last_some_avg10", 0.0))
        prev_full = float(state.get("last_full_avg10", 0.0))
        last_freeze_at = float(state.get("last_freeze_at", 0.0))

        if pressure is None:
            some = 0.0
            full = 0.0
        else:
            some = float(pressure.some_avg10)
            full = float(pressure.full_avg10)

        new_active = was_active
        if some < PRESSURE_CLEAR_SOME_AVG10 and full < PRESSURE_CLEAR_FULL_AVG10:
            frozen = [
                h
                for h in live_holders
                if h.get("frozen")
                and isinstance(h.get("cgroup_path"), str)
                and h.get("cgroup_path")
            ]
            if frozen:
                # Thaw oldest frozen gate first.
                target = min(
                    frozen,
                    key=lambda h: (float(h.get("started_at", 0.0)), str(h.get("holder_id", ""))),
                )
                if self._thaw(Path(str(target["cgroup_path"])), str(target.get("unit_name", ""))):
                    target["frozen"] = False
                    hid = str(target["holder_id"])
                    _atomic_write_json(self.holders_dir / f"{hid}.json", target)
                    frozen.remove(target)
            if not any(bool(h.get("frozen")) for h in live_holders):
                new_active = False
        elif some >= PRESSURE_APPEARS_SOME_AVG10 or full >= PRESSURE_APPEARS_FULL_AVG10:
            new_active = True
            rising = (some > prev_some) or (full > prev_full)
            critical = full >= PRESSURE_CRITICAL_FULL_AVG10
            above_freeze = (
                some >= PRESSURE_FREEZE_SOME_AVG10 or full >= PRESSURE_FREEZE_FULL_AVG10
            )
            cooldown_ok = (now - last_freeze_at) >= self._freeze_cooldown
            if was_active and above_freeze and (rising or critical) and cooldown_ok:
                candidates = [
                    h
                    for h in live_holders
                    if not h.get("frozen")
                    and isinstance(h.get("cgroup_path"), str)
                    and bool(h.get("cgroup_path"))
                ]
                if candidates:
                    # Freeze most recently started gate.
                    newest = max(
                        candidates,
                        key=lambda h: (
                            float(h.get("started_at", 0.0)),
                            str(h.get("holder_id", "")),
                        ),
                    )
                    if self._freeze(
                        Path(str(newest["cgroup_path"])), str(newest.get("unit_name", ""))
                    ):
                        newest["frozen"] = True
                        count_val = newest.get("freeze_count", 0)
                        newest["freeze_count"] = (
                            int(count_val) if isinstance(count_val, int) else 0
                        ) + 1
                        hid = str(newest["holder_id"])
                        _atomic_write_json(self.holders_dir / f"{hid}.json", newest)
                        last_freeze_at = now
        new_state = {
            "pressure_active": new_active,
            "last_some_avg10": some,
            "last_full_avg10": full,
            "last_freeze_at": last_freeze_at,
            "updated_at": now,
        }
        _atomic_write_json(self.pressure_state_path, new_state)
        return new_state

    def step_feedback(self) -> dict[str, Any]:
        """Run one PSI pressure & freeze/thaw coordination pass under the ledger lock."""
        now = float(self._wall_clock())
        with self._locked():
            live = self._reap_dead_holders_locked()
            return self._evaluate_pressure_locked(now, live)

    def update_holder(self, holder_id: str, **fields: Any) -> None:
        meta_path = self.holders_dir / f"{holder_id}.json"
        with self._locked():
            record = _read_json_dict(meta_path)
            if record is None:
                return
            record.update(fields)
            _atomic_write_json(meta_path, record)

    def release_holder(
        self, holder_id: str, lock_file: IO[str], meta_path: Path, lock_path: Path
    ) -> None:
        try:
            with self._locked():
                meta_path.unlink(missing_ok=True)
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
                finally:
                    lock_file.close()
                lock_path.unlink(missing_ok=True)
        except OSError:
            with contextlib.suppress(OSError):
                lock_file.close()

    def acquire(
        self,
        step: str,
        size_bucket: str = "default",
        *,
        worktree: str = "",
        wait: float | None = None,
        notify: Callable[[str], None] = print,
    ) -> MemoryLease:
        """Reserve ``step:size_bucket`` predicted peak, waiting on pressure or budget."""
        budget_wait = resolve_wait_seconds(wait)
        guard_enabled = memory_guard_enabled()
        self.root.mkdir(parents=True, exist_ok=True)
        self.holders_dir.mkdir(parents=True, exist_ok=True)
        holder_id = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        lock_path = self.holders_dir / f"{holder_id}.lock"
        meta_path = self.holders_dir / f"{holder_id}.json"
        lock_file = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            lock_file.close()
            raise

        started_mono = float(self._clock())
        next_notice = started_mono
        try:
            while True:
                avail = self._avail_mem()
                now_wall = float(self._wall_clock())
                blocked_reason = ""
                with self._locked():
                    live = self._reap_dead_holders_locked(exclude_holder_id=holder_id)
                    pstate = self._evaluate_pressure_locked(now_wall, live)
                    predicted = self._predict_peak_locked(step, size_bucket)
                    remaining = self._remaining_budget_locked(avail, live)
                    if guard_enabled and pstate.get("pressure_active"):
                        blocked_reason = "memory pressure active; holding new grants"
                    elif guard_enabled and remaining is not None and predicted > remaining:
                        blocked_reason = (
                            f"step={step}:{size_bucket} predicted={predicted} bytes "
                            f"> budget={remaining} bytes ({len(live)} active)"
                        )
                    else:
                        grant = predicted
                        record: dict[str, Any] = {
                            "holder_id": holder_id,
                            "pid": os.getpid(),
                            "worktree": worktree,
                            "step": step,
                            "size_bucket": size_bucket,
                            "grant_bytes": grant,
                            "started_at": now_wall,
                            "unit_name": "",
                            "cgroup_path": "",
                            "frozen": False,
                            "freeze_count": 0,
                        }
                        _atomic_write_json(meta_path, record)
                        return MemoryLease(
                            self,
                            holder_id,
                            lock_file,
                            meta_path,
                            lock_path,
                            step=step,
                            size_bucket=size_bucket,
                            grant_bytes=grant,
                            wait_seconds=max(0.0, float(self._clock()) - started_mono),
                        )
                now_mono = float(self._clock())
                if now_mono >= next_notice:
                    notify(f"waiting for memory budget: {blocked_reason}")
                    if notify is print:
                        sys.stdout.flush()
                    next_notice = now_mono + 60.0
                elapsed = now_mono - started_mono
                if elapsed >= budget_wait:
                    raise TimeoutError(f"MEMORY_BUDGET_TIMEOUT: {blocked_reason}")
                self._sleep(min(0.1, max(0.0, budget_wait - elapsed)))
        except BaseException:
            with contextlib.suppress(OSError):
                meta_path.unlink(missing_ok=True)
                fcntl.flock(lock_file, fcntl.LOCK_UN)
                lock_file.close()
                lock_path.unlink(missing_ok=True)
            raise


_STOPPED_EXIT_CODES = frozenset({137, 143})


def run_with_lease(
    *,
    ledger: MemoryLedger,
    step: str,
    size_bucket: str,
    invoke: Callable[[list[str], dict[str, str], int, int], tuple[int, bool, bool, Any]],
    cmd: list[str],
    worktree: str = "",
    env: dict[str, str] | None = None,
    wait: float | None = None,
    notify: Callable[[str], None] = print,
    existing_lease: MemoryLease | None = None,
    external_grant_bytes: int | None = None,
    compute_workers: bool = False,
    record_peak: bool = True,
    rerun_if_stopped: bool = False,
    max_reruns: int = MAX_STOPPED_RERUNS,
    scope_prefix_fn: Callable[[int, str, Path], list[str]] | None = None,
    on_lease: Callable[[MemoryLease | None], None] | None = None,
) -> tuple[Any, MemoryRunStats]:
    """Execute ``invoke(scoped_cmd, env_updates, grant_bytes, workers)`` under a memory lease.

    Handles ledger reservation, systemd scope wrapping with ``MemoryHigh=grant``,
    background PSI feedback monitoring, measured ``memory.peak`` learning, and
    re-running stopped test invocations without misreporting failure.
    """
    prefix_builder = scope_prefix_fn or (
        lambda g, unit, meta: build_cgroup_scope_prefix(g, unit_name=unit, meta_dir=meta)
    )
    attempt = 0
    total_wait = 0.0
    total_freezes = 0
    best_peak = 0
    last_oom = 0
    last_grant = 0
    last_workers = 1

    while True:
        lease: MemoryLease | None = existing_lease
        owns_lease = False
        if lease is None:
            if external_grant_bytes is not None and external_grant_bytes > 0:
                last_grant = int(external_grant_bytes)
            elif os.environ.get(EXTERNAL_LEASE_ENV) == "1":
                raw_grant = os.environ.get(GRANTED_MEMORY_ENV, "").strip()
                last_grant = (
                    int(raw_grant)
                    if raw_grant.isdigit() and int(raw_grant) > 0
                    else ledger.predict_peak(step, size_bucket)
                )
            else:
                lease = ledger.acquire(
                    step, size_bucket, worktree=worktree, wait=wait, notify=notify
                )
                owns_lease = True
                last_grant = lease.grant_bytes
                total_wait += lease.wait_seconds
        else:
            last_grant = lease.grant_bytes
            total_wait = lease.wait_seconds
        if on_lease is not None:
            on_lease(lease)

        workers = (
            ledger.workers_for_grant(last_grant, step, size_bucket) if compute_workers else 1
        )
        last_workers = workers

        meta_base = disk_scratch_base()
        meta_base.mkdir(parents=True, exist_ok=True)
        meta_dir = Path(tempfile.mkdtemp(prefix="saberops-cg-meta-", dir=str(meta_base)))
        unit_name = f"saberops-gate-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        prefix = prefix_builder(last_grant, unit_name, meta_dir)
        scoped_cmd = [*prefix, *cmd] if prefix else list(cmd)

        env_updates = dict(env or {})
        env_updates.setdefault(GRANTED_MEMORY_ENV, str(last_grant))
        env_updates.setdefault(GRANTED_WORKERS_ENV, str(workers))
        for tmp_key in ("TMPDIR", "TMP", "TEMP"):
            if tmp_key not in env_updates:
                current_val = os.environ.get(tmp_key, "").strip()
                if not current_val or is_tmpfs_path(Path(current_val)):
                    env_updates[tmp_key] = str(meta_base)

        stop_monitor = threading.Event()
        observed_peak = 0
        published_cg = False

        def monitor_loop(
            _meta_dir: Path = meta_dir,
            _stop_monitor: threading.Event = stop_monitor,
            _lease: MemoryLease | None = lease,
            _unit_name: str = unit_name,
        ) -> None:
            nonlocal observed_peak, published_cg
            cg_file = _meta_dir / "cgroup_path"
            cg_dir: Path | None = None
            while not _stop_monitor.wait(0.25):
                if cg_dir is None and cg_file.is_file():
                    try:
                        raw_cg = cg_file.read_text(encoding="utf-8", errors="replace").strip()
                    except OSError:
                        raw_cg = ""
                    if raw_cg:
                        cg_dir = Path(raw_cg)
                        if _lease is not None and not published_cg:
                            _lease.set_cgroup_path(cg_dir, unit_name=_unit_name)
                            published_cg = True
                if cg_dir is not None:
                    pk = read_cgroup_peak(cg_dir)
                    if pk is not None and pk > observed_peak:
                        observed_peak = pk
                with contextlib.suppress(OSError):
                    ledger.step_feedback()

        monitor = threading.Thread(target=monitor_loop, daemon=True)
        monitor.start()
        timed_out = False
        interrupted = False
        exit_code = -1
        payload: Any = None
        try:
            exit_code, timed_out, interrupted, payload = invoke(
                scoped_cmd, env_updates, last_grant, workers
            )
        finally:
            stop_monitor.set()
            monitor.join(timeout=2.0)

            peak_file = meta_dir / "memory.peak"
            if peak_file.is_file():
                try:
                    file_peak = int(peak_file.read_text(encoding="utf-8").strip())
                    if file_peak > observed_peak:
                        observed_peak = file_peak
                except (OSError, ValueError):
                    pass
            events_file = meta_dir / "memory.events"
            oom_kill = 0
            if events_file.is_file():
                with contextlib.suppress(OSError):
                    oom_kill = parse_memory_events_oom_kill(
                        events_file.read_text(encoding="utf-8", errors="replace")
                    )
            if lease is not None:
                total_freezes += lease.freeze_count()
            shutil.rmtree(meta_dir, ignore_errors=True)
            if owns_lease and lease is not None:
                lease.release()

        last_oom = oom_kill
        if observed_peak > best_peak:
            best_peak = observed_peak
        if record_peak and observed_peak > 0:
            ledger.record_peak(
                step,
                size_bucket,
                observed_peak,
                workers=workers if compute_workers else None,
            )

        was_stopped = (
            not timed_out
            and not interrupted
            and (exit_code < 0 or exit_code in _STOPPED_EXIT_CODES or oom_kill > 0)
        )
        if rerun_if_stopped and was_stopped and attempt < max_reruns:
            attempt += 1
            notify(
                f"[memory-budget] step={step} was stopped (exit={exit_code} oom={oom_kill}); "
                f"waiting and re-running ({attempt}/{max_reruns})"
            )
            continue

        stats = MemoryRunStats(
            step=step,
            size_bucket=size_bucket,
            grant_bytes=last_grant,
            measured_peak_bytes=best_peak,
            wait_seconds=total_wait,
            freeze_count=total_freezes,
            workers=last_workers,
            oom_kill=last_oom,
            rerun_count=attempt,
        )
        return payload, stats


def wait_process_with_freeze_pause(
    child: subprocess.Popen[str],
    *,
    timeout: float | None = None,
    lease: MemoryLease | None = None,
    is_frozen: Callable[[], bool] | None = None,
    on_poll: Callable[[], None] | None = None,
) -> tuple[int, bool]:
    """Wait for ``child``, pausing the wall-timeout clock while ``lease`` is frozen."""
    active_elapsed = 0.0
    last_tick = time.monotonic()
    timed_out = False
    while child.poll() is None:
        if on_poll is not None:
            on_poll()
        now = time.monotonic()
        dt = max(0.0, now - last_tick)
        last_tick = now
        frozen_now = (
            is_frozen()
            if is_frozen is not None
            else (lease is not None and lease.is_frozen())
        )
        if not frozen_now:
            active_elapsed += dt
        if timeout is not None and active_elapsed >= timeout:
            timed_out = True
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                child.wait()
            break
        time.sleep(0.05)
    if on_poll is not None:
        on_poll()
    return (124 if timed_out else child.returncode), timed_out



def lane_targets(repo: Path, changed: list[str], also: list[str]) -> list[str]:
    targets = set(also)
    for name in changed:
        path = Path(name)
        if name.startswith("tests/") and path.name.startswith("test_") and path.suffix == ".py":
            if (repo / path).is_file():
                targets.add(name)
        elif name.startswith("src/saberops/") and path.suffix == ".py":
            test = f"tests/test_{path.stem}.py"
            if (repo / test).is_file():
                targets.add(test)
    return sorted(targets)


@dataclass
class Evidence:
    codes: list[int] = field(default_factory=list)
    log: str = ""
    cases: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    passed: set[str] = field(default_factory=set)
    count: int = 0


def junit(path: Path) -> Evidence:
    result = Evidence()
    if not path.exists():
        return result
    tree = ET.parse(path)
    for case in tree.iter("testcase"):
        if not case.get("classname") and case.get("name", "").endswith(".py"):
            node = case.get("name", "")
        else:
            node = ""
        filename = case.get("file") or case.get("classname", "").replace(".", "/") + ".py"
        node = node or filename + "::" + case.get("name", "")
        # xunit2 classname includes class names; split at the test module.
        if case.get("classname") and (
            ".py" not in filename or not Path(filename).name.startswith("test_")
        ):
            parts = case.get("classname", "").split(".")
            module = next((i for i, p in enumerate(parts) if p.startswith("test_")), len(parts) - 1)
            node = "/".join(parts[: module + 1]) + ".py::"
            node += "::".join([*parts[module + 1 :], case.get("name", "")])
        error = case.find("failure")
        if error is None:
            error = case.find("error")
        text = "" if error is None else "".join(error.itertext())
        result.cases[node] = text
        if error is not None:
            result.failed[node] = text
        elif case.find("skipped") is None:
            result.passed.add(node)
        result.count += 1
    return result


def evaluate(
    initial: Evidence,
    rerun: Evidence | None = None,
    quarantine: set[str] | None = None,
    checks_ok: bool = True,
    selected: bool = True,
) -> tuple[str, str, int, list[str], list[str]]:
    all_evidence = [initial] + ([rerun] if rerun else [])
    logs = "\n".join(e.log for e in all_evidence)
    codes = [c for e in all_evidence for c in e.codes]
    if "No space left on device" in logs or "Errno 28" in logs:
        return "INFRA", "ENOSPC", 3, sorted(initial.failed), []
    if 3 in codes or "INTERNALERROR" in logs:
        return "INFRA", "PYTEST_INTERNAL", 3, sorted(initial.failed), []
    if any(c < 0 or c in (130, 143) for c in codes):
        return "INFRA", "INTERRUPTED", 3, sorted(initial.failed), []
    flaky = sorted(set(initial.failed) & rerun.passed) if rerun else []
    final = sorted(
        (set(initial.failed) - rerun.passed) | set(rerun.failed) if rerun else initial.failed
    )
    if 124 in codes or (
        final
        and any(
            "Timeout" in e.failed.get(n, "") or "timeout" in e.failed.get(n, "")
            for e in all_evidence
            for n in final
        )
    ):
        return "TIMEOUT", "", 1, final, flaky
    if selected and initial.count == 0 and all(c in (0, 5) for c in codes):
        return "FAIL", "SELECTED_BUT_ZERO_RAN", 5, final, flaky
    unquarantined = sorted(set(flaky) - (quarantine or set()))
    bad_initial = any(c not in (0, 1, 5) for c in initial.codes)
    bad_rerun = rerun is not None and any(c != 0 for c in rerun.codes)
    if not checks_ok or final or unquarantined or bad_initial or bad_rerun:
        reason = "unquarantined: " + ", ".join(unquarantined) if unquarantined else ""
        return "FAIL", reason, 1, final, flaky
    if any(c == 1 for c in initial.codes) and not initial.failed:
        return "FAIL", "pytest failed without rerunnable node IDs", 1, final, flaky
    return ("FLAKY_PASS" if flaky else "PASS"), "", 0, final, flaky


def history_path(common: Path) -> Path:
    """Full-gate history file shared by all worktrees of one checkout.

    Covers: ADR-0003 D5 (flaky tracking history format).
    """
    return common / HISTORY_DIRNAME / HISTORY_FILENAME


def append_history(path: Path, entry: dict[str, Any]) -> None:
    """Append one JSON line with an exclusive flock; warn, never raise.

    Covers: prompt section A (history append is best-effort).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
                handle.flush()
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError as error:
        print(f"warning: could not write gate history: {error}", flush=True)


def load_history(path: Path) -> list[dict[str, Any]]:
    """Read full.jsonl oldest-first; missing file or bad lines yield [].

    Covers: prompt section B (flaky-report input).
    """
    if not path.exists():
        return []
    try:
        text = path.read_text()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def read_quarantine(repo: Path) -> set[str]:
    """Exact quarantined node IDs from tests/quarantine.txt.

    Covers: prompt section B (quarantine marking is display-only).
    """
    quarantine_file = repo / "tests/quarantine.txt"
    if not quarantine_file.exists():
        return set()
    try:
        text = quarantine_file.read_text()
    except OSError:
        return set()
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def flaky_stats(
    entries: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """Per-test flaky streak/total/last-seen plus the newest main-gate entry.

    The streak counts consecutive non-INFRA full gates (newest first) in
    which the test was flaky; INFRA entries neither break nor extend it.
    Totals and last-seen likewise count non-INFRA gates only.

    Covers: prompt section B (streak counting newest-first, INFRA skipped).
    """
    per_test: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry.get("outcome") == "INFRA":
            continue
        flaky = entry.get("flaky", [])
        if not isinstance(flaky, list):
            continue
        ts = str(entry.get("ts", ""))
        for node in flaky:
            if not isinstance(node, str):
                continue
            record = per_test.setdefault(node, {"total": 0, "last_seen": ""})
            record["total"] = int(record["total"]) + 1
            # Entries are oldest-first, so the newest flaky ts wins.
            record["last_seen"] = ts
    for node in per_test:
        streak = 0
        for entry in reversed(entries):
            if entry.get("outcome") == "INFRA":
                continue
            flaky = entry.get("flaky", [])
            if not isinstance(flaky, list):
                break
            if node in flaky:
                streak += 1
            else:
                break
        per_test[node]["streak"] = streak
    newest_main_gate: dict[str, Any] | None = None
    for entry in reversed(entries):
        if entry.get("label") in ("main-gate", "nightly"):
            newest_main_gate = entry
            break
    return per_test, newest_main_gate


def render_flaky_report(
    history_file: Path,
    entries: list[dict[str, Any]],
    min_streak: int,
    quarantine: set[str],
    as_json: bool,
) -> str:
    """Render the human markdown report or the --json payload.

    Covers: prompt section B (quarantine proposals + main-gate failures).
    """
    per_test, newest_main_gate = flaky_stats(entries)
    proposals = sorted(
        (node for node, info in per_test.items() if int(info.get("streak", 0)) >= min_streak)
    )
    main_gate_failed: list[str] = []
    if newest_main_gate is not None:
        failed = newest_main_gate.get("failed", [])
        if isinstance(failed, list):
            main_gate_failed = sorted(n for n in failed if isinstance(n, str))
    if as_json:
        payload = {
            "history": str(history_file),
            "entries": len(entries),
            "min_streak": min_streak,
            "proposals": [
                {
                    "id": node,
                    "streak": int(per_test[node].get("streak", 0)),
                    "total": int(per_test[node].get("total", 0)),
                    "last_seen": str(per_test[node].get("last_seen", "")),
                    "quarantined": node in quarantine,
                }
                for node in proposals
            ],
            "main_gate_failures": main_gate_failed,
            "main_gate": newest_main_gate,
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    lines = ["# Flaky report", ""]
    lines.append(f"history: {history_file} ({len(entries)} full gates)")
    lines.append("")
    lines.append(f"## quarantine proposals (streak >= {min_streak})")
    if not proposals:
        lines.append("(none)")
    else:
        for node in proposals:
            info = per_test[node]
            suffix = " [in quarantine]" if node in quarantine else ""
            lines.append(
                f"- {node} (streak={info.get('streak')}, "
                f"total={info.get('total')}, last={info.get('last_seen')}){suffix}"
            )
    lines.append("")
    lines.append("## main-gate failures")
    if newest_main_gate is None:
        lines.append("(no main-gate run yet)")
    elif not main_gate_failed:
        failed_ts = newest_main_gate.get("ts", "")
        failed_sha = newest_main_gate.get("head_sha", "")
        lines.append(f"(none; newest main gate {failed_ts} {failed_sha})")
    else:
        lines.append(f"newest main gate {newest_main_gate.get('ts', '')} "
                     f"{newest_main_gate.get('head_sha', '')}:")
        for node in main_gate_failed:
            lines.append(f"- {node}")
    lines.append("")
    return "\n".join(lines)


def flaky_report_cli(argv: list[str]) -> int:
    """Entry point for `gate_tools.py flaky-report [--min-streak N] [--json]`.

    Covers: prompt section B (flaky-report CLI).
    """
    parser = argparse.ArgumentParser(prog="gate_tools.py flaky-report")
    parser.add_argument("--min-streak", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    parsed = parser.parse_args(argv)
    if parsed.min_streak < 1:
        parser.error("--min-streak must be >= 1")
    repo = Path.cwd()
    try:
        common = (repo / git(repo, "rev-parse", "--git-common-dir")).resolve()
        history_file = history_path(common)
    except subprocess.CalledProcessError:
        history_file = Path.cwd() / HISTORY_DIRNAME / HISTORY_FILENAME
    entries = load_history(history_file)
    quarantine = read_quarantine(repo)
    report = render_flaky_report(history_file, entries, parsed.min_streak, quarantine, parsed.json)
    print(report, end="")
    return 0


def _run_passthrough_step(
    *,
    repo: Path,
    common: Path | None,
    step: str,
    size_bucket: str,
    cmd: list[str],
    compute_workers: bool = False,
    append_workers_flag: bool = False,
    timeout: float | None = None,
    rerun_if_stopped: bool = False,
    extra_env: dict[str, str] | None = None,
) -> int:
    """Acquire a ledger reservation (unless external), run ``cmd``, print visibility."""
    ledger = MemoryLedger(default_ledger_root(common, repo=repo))
    lease_ref: list[MemoryLease | None] = [None]

    def invoke(
        scoped_cmd: list[str], env_updates: dict[str, str], _grant: int, workers: int
    ) -> tuple[int, bool, bool, int]:
        effective_cmd = list(scoped_cmd)
        if append_workers_flag and workers > 1 and "-n" not in effective_cmd:
            effective_cmd.extend(["-n", str(workers), "--dist", "worksteal"])
        child_env = {**os.environ, **env_updates}
        child = subprocess.Popen(
            effective_cmd,
            cwd=repo,
            env=child_env,
            text=True,
            start_new_session=True,
        )
        code, timed_out = wait_process_with_freeze_pause(
            child,
            timeout=timeout,
            is_frozen=lambda: bool(lease_ref[0] is not None and lease_ref[0].is_frozen()),
        )
        return code, timed_out, False, code

    try:
        rc, stats = run_with_lease(
            ledger=ledger,
            step=step,
            size_bucket=size_bucket,
            invoke=invoke,
            cmd=cmd,
            worktree=str(repo),
            env=extra_env,
            compute_workers=compute_workers,
            rerun_if_stopped=rerun_if_stopped,
            on_lease=lambda active: lease_ref.__setitem__(0, active),
        )
    except TimeoutError:
        print("INFRA SLOT_WAIT_TIMEOUT", flush=True)
        return 3
    print(stats.format_line(), flush=True)
    return int(rc)


def exclusive_cli(argv: list[str]) -> int:
    """Run CMD under a host-wide memory reservation for coverage-map rebuild."""
    cmd: list[str] = list(argv[1:]) if argv[:1] == ["--"] else list(argv)
    if not cmd or cmd == ["--"]:
        print("usage: gate_tools.py exclusive -- CMD [ARGS...]", file=sys.stderr)
        return 2
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("usage: gate_tools.py exclusive -- CMD [ARGS...]", file=sys.stderr)
        return 2
    repo = Path.cwd()
    try:
        common = (repo / git(repo, "rev-parse", "--git-common-dir")).resolve()
    except subprocess.CalledProcessError:
        print("INFRA SLOT_WAIT_TIMEOUT (not a git repository)", flush=True)
        return 3
    return _run_passthrough_step(
        repo=repo,
        common=common,
        step="coverage-map",
        size_bucket="full",
        cmd=cmd,
        compute_workers=True,
        rerun_if_stopped=True,
        extra_env={EXTERNAL_LEASE_ENV: "1"},
    )


def step_cli(argv: list[str]) -> int:
    """Run a heavy gate step under the host-wide memory ledger and cgroup ceiling."""
    parser = argparse.ArgumentParser(prog="gate_tools.py step")
    parser.add_argument("--step", required=True)
    parser.add_argument("--size-count", type=int, default=None)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--with-workers", action="store_true")
    parser.add_argument("--compute-workers", action="store_true")
    parser.add_argument("--rerun-if-stopped", action="store_true")
    parser.add_argument("--timeout", type=float, default=None)
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(argv)
    cmd = list(parsed.cmd)
    if cmd[:1] == ["--"]:
        cmd = cmd[1:]
    if not cmd:
        print("usage: gate_tools.py step --step STEP -- CMD [ARGS...]", file=sys.stderr)
        return 2
    repo = Path.cwd()
    common: Path | None = None
    with contextlib.suppress(subprocess.CalledProcessError):
        common = (repo / git(repo, "rev-parse", "--git-common-dir")).resolve()
    bucket = selection_size_bucket(parsed.step, parsed.size_count, is_full=parsed.full)
    return _run_passthrough_step(
        repo=repo,
        common=common,
        step=parsed.step,
        size_bucket=bucket,
        cmd=cmd,
        compute_workers=bool(parsed.with_workers or parsed.compute_workers),
        append_workers_flag=bool(parsed.with_workers),
        timeout=parsed.timeout,
        rerun_if_stopped=bool(parsed.rerun_if_stopped),
    )


class Interrupted(Exception):
    """A gate signal, after the child process group is stopped."""


class Runner:
    def __init__(self, repo: Path, kind: str, result_dir: Path | None = None) -> None:
        self.repo = repo
        self.kind = kind
        self.started = time.monotonic()
        self.common = (repo / git(repo, "rev-parse", "--git-common-dir")).resolve()
        user = os.environ.get("USER", str(os.getuid()))
        self.root = gate_scratch_root()
        self.root.mkdir(parents=True, exist_ok=True)
        scratch_base = disk_scratch_base()
        sweep(self.root, scratch_base / f"pytest-of-{user}")
        legacy_tmp = Path(f"/tmp/pytest-of-{user}")
        if legacy_tmp != scratch_base / f"pytest-of-{user}":
            sweep(self.root, legacy_tmp)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        self.run = self.root / f"{repo.name}-{stamp}-{os.getpid()}"
        self.run.mkdir()
        self.tmp = self.run / "tmp"
        self.tmp.mkdir(exist_ok=True)
        self.record = {
            "pid": os.getpid(),
            "worktree": str(repo),
            "started_at": datetime.now(UTC).isoformat(),
            "kind": kind,
        }
        (self.run / "owner.json").write_text(json.dumps(self.record))
        self.result = result_dir or self.run / "result"
        self.result.mkdir(parents=True, exist_ok=True)
        self.python = (
            str(repo / ".venv/bin/python")
            if (repo / ".venv/bin/python").exists()
            else sys.executable
        )
        self.child: subprocess.Popen[str] | None = None
        self.peak = 0
        self.test_count = 0
        self.heavy: list[tuple[int, str]] = []
        self.ledger = MemoryLedger(default_ledger_root(self.common, repo=self.repo))
        self._active_lease: MemoryLease | None = None
        self._interrupted = False
        self.memory_step = "full" if kind == "full" else "affected"
        self.memory_bucket = "full" if kind == "full" else "default"
        self.memory_grant_bytes = 0
        self.memory_peak_bytes = 0
        self.memory_wait_seconds = 0.0
        self.memory_freeze_count = 0
        self.memory_workers = 1
        self._next_cmd_meta: tuple[str, str, bool, bool, bool] | None = None
        self.stop_monitor = threading.Event()
        self.monitor = threading.Thread(target=self.measure_loop, daemon=True)
        self.monitor.start()
        # Engine-evidence context (kind/producer/profile) is filled in by
        # run_checks; defaults keep direct/legacy calls honest.
        self.checks_ok = True
        self.selected = False
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, self.interrupt)

    def history_kind(self) -> str:
        """Lifecycle kind recorded in the history ledger (ADR-0004 D3)."""
        explicit = os.environ.get(KIND_ENV, "").strip()
        if explicit in KNOWN_KINDS:
            return explicit
        if self.kind == "lane":
            return "G1"
        label = os.environ.get(GATE_LABEL_ENV, DEFAULT_GATE_LABEL)
        return label if label == "main-gate" else "manual"

    def producer(self) -> str:
        """Evidence producer; engine-run gates record ``engine``."""
        return os.environ.get(PRODUCER_ENV, "").strip() or DEFAULT_PRODUCER

    def profile_name(self) -> str:
        """Gate profile this run corresponds to (ADR-0003 D2)."""
        explicit = os.environ.get(PROFILE_ENV, "").strip()
        if explicit:
            return explicit
        kind = self.history_kind()
        if kind in ("G1", "G2"):
            return "fast"
        if kind == "full":
            return "full"
        return "full" if self.kind == "full" else "fast"

    def interrupt(self, signum: int, frame: Any) -> None:
        self._interrupted = True
        if self.child is not None and self.child.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.child.pid, signal.SIGTERM)
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.child.pid, signal.SIGKILL)
                self.child.wait()
        raise Interrupted()

    def measure_loop(self) -> None:
        while not self.stop_monitor.wait(0.5):
            try:
                self.measure()
            except OSError:
                pass  # pytest removes successful tmp_path directories concurrently

    def measure(self) -> None:
        value = size(self.run)
        if value >= self.peak:
            self.peak = value
            self.heavy = sorted(
                [
                    (size(p), str(p.relative_to(self.run)))
                    for base in self.run.glob("pytest-*")
                    for p in test_directories(base)
                    if p.is_dir() and not p.is_symlink()
                ],
                reverse=True,
            )[:10]

    def command(self, args: list[str], name: str, timeout: float | None = None) -> tuple[int, str]:
        meta = self._next_cmd_meta
        self._next_cmd_meta = None
        step, bucket, compute_workers, do_record, rerun_stopped = meta or (
            self.memory_step,
            self.memory_bucket,
            False,
            False,
            False,
        )
        print("+ " + " ".join(args), flush=True)
        logfile = self.result / f"{name}.log"
        lease_ref: list[MemoryLease | None] = [self._active_lease]

        def invoke(
            scoped_cmd: list[str], env_updates: dict[str, str], _grant: int, _workers: int
        ) -> tuple[int, bool, bool, int]:
            child_env = {
                **os.environ,
                **env_updates,
                "TMPDIR": str(self.tmp),
                "TMP": str(self.tmp),
                "TEMP": str(self.tmp),
            }
            with logfile.open("w") as output:
                self.child = subprocess.Popen(
                    scoped_cmd,
                    cwd=self.repo,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=child_env,
                    start_new_session=True,
                )
                with logfile.open() as reader:

                    def pump_output() -> None:
                        text = reader.read()
                        if text:
                            print(text, end="", flush=True)

                    code, timed_out = wait_process_with_freeze_pause(
                        self.child,
                        timeout=timeout,
                        is_frozen=lambda: bool(
                            lease_ref[0] is not None and lease_ref[0].is_frozen()
                        ),
                        on_poll=pump_output,
                    )
                    pump_output()
            self.child = None
            return code, timed_out, self._interrupted, code

        ext_grant = self.memory_grant_bytes if self.memory_grant_bytes > 0 else None
        code, stats = run_with_lease(
            ledger=self.ledger,
            step=step,
            size_bucket=bucket,
            invoke=invoke,
            cmd=args,
            worktree=str(self.repo),
            existing_lease=self._active_lease,
            external_grant_bytes=ext_grant,
            compute_workers=compute_workers,
            record_peak=do_record,
            rerun_if_stopped=rerun_stopped,
            on_lease=lambda active: lease_ref.__setitem__(0, active),
        )
        self.memory_peak_bytes = max(self.memory_peak_bytes, stats.measured_peak_bytes)
        active_now = lease_ref[0]
        if active_now is not None:
            self.memory_freeze_count = max(
                self.memory_freeze_count, active_now.freeze_count()
            )
        else:
            self.memory_freeze_count += stats.freeze_count
        return int(code), logfile.read_text(errors="replace")

    def pytest(self, name: str, args: list[str], budget: float | None = None) -> Evidence:
        path = self.result / f"{name}.xml"
        argv = [
            self.python,
            "-m",
            "pytest",
            "-q",
            "--durations=20",
            "--junitxml",
            str(path),
            "--basetemp",
            str(self.run / f"pytest-{name}"),
            *args,
        ]
        self._next_cmd_meta = (self.memory_step, self.memory_bucket, True, True, True)
        code, log = self.command(argv, name, budget)
        try:
            evidence = junit(path)
        except ET.ParseError:
            evidence = Evidence()
            if code in (0, 1, 5):
                code = 3
        evidence.codes = [code]
        evidence.log = log
        return evidence

    def finish(self, verdict: tuple[str, str, int, list[str], list[str]], count: int) -> int:
        outcome, reason, code, failed, flaky = verdict
        self.stop_monitor.set()
        self.monitor.join()
        self.measure()
        wall = round(time.monotonic() - self.started, 2)
        if self._active_lease is not None:
            self.memory_freeze_count = max(
                self.memory_freeze_count, self._active_lease.freeze_count()
            )
        mem_stats = MemoryRunStats(
            step=self.memory_step,
            size_bucket=self.memory_bucket,
            grant_bytes=self.memory_grant_bytes,
            measured_peak_bytes=self.memory_peak_bytes,
            wait_seconds=self.memory_wait_seconds,
            freeze_count=self.memory_freeze_count,
            workers=self.memory_workers,
        )
        print(mem_stats.format_line(), flush=True)
        print(f"run dir {self.run}: {size(self.run)} bytes; peak {self.peak} bytes")
        print("10 largest test dirs at peak:")
        for amount, name in self.heavy:
            print(f"  {amount} {name}")
        (self.result / "failed.txt").write_text("".join(n + "\n" for n in failed))
        (self.result / "flaky.txt").write_text("".join(n + "\n" for n in flaky))
        payload = {
            "outcome": outcome,
            "reason": reason,
            "counts": {"tests": count, "failed": len(failed), "flaky": len(flaky)},
            "wall_time": wall,
            "head_sha": git(self.repo, "rev-parse", "HEAD"),
            "peak_bytes": self.peak,
            "heavy_dirs": self.heavy,
            "kind": self.history_kind(),
            "producer": self.producer(),
            "profile": self.profile_name(),
            "checks_ok": self.checks_ok,
            "selected": self.selected,
            "memory_budget": mem_stats.as_dict(),
        }
        (self.result / "outcome.json").write_text(json.dumps(payload, indent=2) + "\n")
        if self.kind == "full" and os.environ.get("SABEROPS_GATE_NO_HISTORY") != "1":
            entry: dict[str, Any] = {
                "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                "head_sha": payload["head_sha"],
                "worktree": str(self.repo),
                "label": os.environ.get(GATE_LABEL_ENV, DEFAULT_GATE_LABEL),
                "kind": self.history_kind(),
                "producer": self.producer(),
                "profile": self.profile_name(),
                "outcome": outcome,
                "reason": reason,
                "tests": count,
                "failed": sorted(failed),
                "flaky": sorted(flaky),
                "wall_time": wall,
                "peak_bytes": self.peak,
                "memory_budget": mem_stats.as_dict(),
            }
            append_history(history_path(self.common), entry)
        if not (self.result / "full.xml").exists():
            ET.ElementTree(ET.Element("testsuites")).write(self.result / "full.xml")
        # Publish evidence before cleaning the private scratch directory.
        if self.kind == "full":
            last = self.common / "saberops-gate-last"
            last.mkdir(parents=True, exist_ok=True)
            for old in [] if last.resolve() == self.result.resolve() else last.iterdir():
                if old.is_file():
                    old.unlink()
            for path in [] if last.resolve() == self.result.resolve() else self.result.iterdir():
                if path.is_file():
                    shutil.copy2(path, last / path.name)
        if os.environ.get("SABEROPS_GATE_KEEP") != "1":
            if outcome in ("FAIL", "TIMEOUT"):
                self.record["failed"] = True
                (self.run / "owner.json").write_text(json.dumps(self.record))
                keep_failed(
                    self.root,
                    str(self.repo),
                    int(float(os.environ.get("SABEROPS_GATE_KEEP_FAILED_GB", "2")) * GB),
                )
            else:
                remove(self.run)
        label = "GATE" if self.kind == "full" else "LANE CHECK"
        suffix = f" ({reason})" if reason else ""
        print(f"{count} tests; {len(failed)} failed; {len(flaky)} flaky; wall {wall}s")
        print(f"{label} OUTCOME: {outcome}{suffix}", flush=True)
        return code

    def _compute_lane_targets(self, base: str, also: list[str]) -> list[str]:
        changed = set(git(self.repo, "diff", "--name-only", f"{base}..HEAD").splitlines())
        for git_args in [
            ("diff", "--name-only"),
            ("diff", "--cached", "--name-only"),
            ("ls-files", "--others", "--exclude-standard"),
        ]:
            changed.update(git(self.repo, *git_args).splitlines())
        print("changed files:\n" + "\n".join(sorted(changed)))
        targets = lane_targets(self.repo, sorted(changed), also)
        print(f"lane targets ({len(targets)}):\n" + "\n".join(targets))
        return targets

    def execute(self, base: str, also: list[str]) -> int:
        verdict: tuple[str, str, int, list[str], list[str]] = ("INFRA", "INTERRUPTED", 3, [], [])
        try:
            minimum = float(os.environ.get("SABEROPS_GATE_MIN_FREE_GB", "10"))
            if not disk_ok([self.root, self.repo], minimum):
                return self.finish(("INFRA", "DISK_LOW", 3, [], []), 0)
            precomputed_targets: list[str] | None = None
            if self.kind == "lane":
                precomputed_targets = self._compute_lane_targets(base, also)
                if precomputed_targets:
                    self.memory_step = "affected"
                    self.memory_bucket = selection_size_bucket(
                        "affected", len(precomputed_targets)
                    )
                else:
                    self.memory_step = "mypy"
                    self.memory_bucket = "default"
            else:
                self.memory_step = "full"
                self.memory_bucket = "full"

            if os.environ.get(EXTERNAL_LEASE_ENV) == "1":
                raw_grant = os.environ.get(GRANTED_MEMORY_ENV, "").strip()
                self.memory_grant_bytes = (
                    int(raw_grant)
                    if raw_grant.isdigit() and int(raw_grant) > 0
                    else self.ledger.predict_peak(self.memory_step, self.memory_bucket)
                )
                self.memory_workers = self.ledger.workers_for_grant(
                    self.memory_grant_bytes, self.memory_step, self.memory_bucket
                )
                print(
                    "host-gates memory lease held by the calling engine; skipping acquisition",
                    flush=True,
                )
                return self.run_checks(base, also, precomputed_targets=precomputed_targets)

            with self.ledger.acquire(
                self.memory_step, self.memory_bucket, worktree=str(self.repo)
            ) as lease:
                self._active_lease = lease
                self.memory_grant_bytes = lease.grant_bytes
                self.memory_wait_seconds = lease.wait_seconds
                self.memory_workers = self.ledger.workers_for_grant(
                    lease.grant_bytes, self.memory_step, self.memory_bucket
                )
                try:
                    return self.run_checks(base, also, precomputed_targets=precomputed_targets)
                finally:
                    self.memory_freeze_count = max(
                        self.memory_freeze_count, lease.freeze_count()
                    )
                    self._active_lease = None
        except TimeoutError:
            verdict = ("INFRA", "SLOT_WAIT_TIMEOUT", 3, [], [])
        except Interrupted:
            pass
        except OSError as error:
            verdict = ("INFRA", "ENOSPC" if error.errno == 28 else "PYTEST_INTERNAL", 3, [], [])
            print(str(error), flush=True)
        return self.finish(verdict, self.test_count)

    def run_checks(
        self,
        base: str,
        also: list[str],
        *,
        precomputed_targets: list[str] | None = None,
    ) -> int:
        targets: list[str] = []
        if self.kind == "lane":
            targets = (
                precomputed_targets
                if precomputed_targets is not None
                else self._compute_lane_targets(base, also)
            )
            if self.memory_grant_bytes <= 0:
                if targets:
                    self.memory_step = "affected"
                    self.memory_bucket = selection_size_bucket("affected", len(targets))
                else:
                    self.memory_step = "mypy"
                    self.memory_bucket = "default"
        else:
            self.memory_step = "full"
            self.memory_bucket = "full"

        if self.memory_grant_bytes <= 0:
            self.memory_grant_bytes = self.ledger.predict_peak(
                self.memory_step, self.memory_bucket
            )
        self.memory_workers = self.ledger.workers_for_grant(
            self.memory_grant_bytes, self.memory_step, self.memory_bucket
        )

        self._next_cmd_meta = (self.memory_step, self.memory_bucket, False, False, False)
        lint, _ = self.command([self.python, "-m", "ruff", "check", "."], "ruff")
        cache = os.environ.get("MYPY_CACHE_DIR", str(self.common / "saberops-mypy-cache"))
        self._next_cmd_meta = ("mypy", "default", False, True, True)
        types, _ = self.command(
            [self.python, "-m", "mypy", "--strict", "--cache-dir", cache, "src", "tests"], "mypy"
        )
        self.checks_ok = lint == 0 and types == 0
        initial = Evidence()
        rerun = None
        if self.kind == "full":
            workers = str(self.memory_workers)
            initial = self.pytest("parallel", ["-n", workers, "--dist", "worksteal"])
            self.test_count = initial.count
            if (
                initial.failed
                and all(c in (0, 1, 5) for c in initial.codes)
                and evaluate(initial)[0] != "INFRA"
            ):
                self.memory_workers = 1
                rerun = self.pytest("rerun", ["-p", "no:xdist", *sorted(initial.failed)])
        elif targets:
            self.selected = True
            args = [*targets]
            if len(targets) > 1 and self.memory_workers > 1:
                workers = str(self.memory_workers)
                args = ["-n", workers, "--dist", "worksteal", *targets]
            initial = self.pytest(
                "lane",
                args,
                float(os.environ.get("SABEROPS_LANE_TIMEOUT", "1200")),
            )
        self.test_count = initial.count
        quarantine_file = self.repo / "tests/quarantine.txt"
        quarantine = (
            {
                line.strip()
                for line in quarantine_file.read_text().splitlines()
                if line.strip() and not line.startswith("#")
            }
            if quarantine_file.exists()
            else set()
        )
        verdict = evaluate(
            initial,
            rerun,
            quarantine,
            lint == 0 and types == 0,
            self.kind == "full" or bool(targets),
        )
        merged = ET.Element("testsuites")
        for path in self.result.glob("*.xml"):
            if path.name != "full.xml":
                try:
                    tree = ET.parse(path).getroot()
                    merged.extend(list(tree) if tree.tag == "testsuites" else [tree])
                except ET.ParseError:
                    pass
        ET.ElementTree(merged).write(self.result / "full.xml", encoding="utf-8")
        return self.finish(verdict, initial.count)


def scratch_cli() -> int:
    """Shared disk-backed scratch lifecycle helper for shell gates."""
    cmd = sys.argv[1]
    if cmd == "disk-tmp":
        print(str(disk_scratch_base()))
        return 0
    if cmd in ("scratch-start", "scratch-start-dir"):
        kind = "ui" if cmd == "scratch-start" else sys.argv[2]
        runner = Runner(Path.cwd(), kind)
        runner.stop_monitor.set()
        runner.monitor.join()
        pid_arg = sys.argv[3] if len(sys.argv) > 3 else str(os.getpid())
        runner.record["pid"] = int(pid_arg)
        (runner.run / "owner.json").write_text(json.dumps(runner.record))
        if cmd == "scratch-start":
            Path(sys.argv[2]).write_text(str(runner.run))
        else:
            print(str(runner.run))
        if not disk_ok(
            [runner.root, runner.repo], float(os.environ.get("SABEROPS_GATE_MIN_FREE_GB", "10"))
        ):
            remove(runner.run)
            return 3
        return 0
    path = Path(sys.argv[2])
    code = int(sys.argv[3])
    if not path.exists():
        return 0
    print(f"run dir {path}: {size(path)} bytes")
    for amount, name in sorted(
        [
            (size(p), str(p))
            for base in path.glob("pytest-*")
            for p in base.iterdir()
            if p.is_dir() and not p.is_symlink()
        ],
        reverse=True,
    )[:10]:
        print(f"  {amount} {name}")
    if os.environ.get("SABEROPS_GATE_KEEP") != "1":
        if code in (0, 3, 130, 143):
            remove(path)
        else:
            record = owner(path)
            record["failed"] = True
            (path / "owner.json").write_text(json.dumps(record))
            keep_failed(
                path.parent,
                str(record.get("worktree", Path.cwd())),
                int(float(os.environ.get("SABEROPS_GATE_KEEP_FAILED_GB", "2")) * GB),
            )
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "flaky-report":
        return flaky_report_cli(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "exclusive":
        return exclusive_cli(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "step":
        return step_cli(sys.argv[2:])
    if len(sys.argv) > 1 and (sys.argv[1].startswith("scratch-") or sys.argv[1] == "disk-tmp"):
        return scratch_cli()
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["full", "lane"])
    parser.add_argument("--base")
    parser.add_argument("--also", nargs="+", action="extend", default=[])
    parser.add_argument("--out", type=Path)
    parser.add_argument("--result-dir", type=Path)
    args = parser.parse_args()
    if args.kind == "lane" and args.base is None:
        parser.error("lane check requires --base REF")
    args.base = args.base or "HEAD"
    if args.out:
        # tee the entire gate including the outcome without buffering a whole run.
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w") as output:
            child = subprocess.Popen(
                [
                    sys.executable,
                    __file__,
                    args.kind,
                    "--base",
                    args.base,
                    *(["--also", *args.also] if args.also else []),
                    *(["--result-dir", str(args.result_dir)] if args.result_dir else []),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, lambda signum, frame: child.send_signal(signum))
            assert child.stdout
            for line in child.stdout:
                print(line, end="", flush=True)
                output.write(line)
                output.flush()
            return child.wait()
    return Runner(Path.cwd(), args.kind, args.result_dir).execute(args.base, args.also)


if __name__ == "__main__":
    raise SystemExit(
        scratch_cli()
        if len(sys.argv) > 1 and (sys.argv[1].startswith("scratch-") or sys.argv[1] == "disk-tmp")
        else main()
    )
