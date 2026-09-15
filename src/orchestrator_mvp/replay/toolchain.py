"""Verifier-toolchain fingerprint for C12-B comparability hardening (F-04).

Derives a deterministic fingerprint from distribution metadata installed under
the dedicated verifier toolchain path (/usr/local/lib/orch-verifier) without
invoking any ambient package manager.

Invariants:
- Same toolchain → same fingerprint.
- Version/package change → different fingerprint.
- Fingerprint stored in replay execution evidence (non-authoritative).
- Mismatched fingerprints must mark runs NOT DIRECTLY COMPARABLE.
- No package-management subsystem is created here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator_mvp.replay.contracts import UNKNOWN, ReplayError

#: Default path for the dedicated verifier toolchain.
VERIFIER_TOOLCHAIN_PATH: str = "/usr/local/lib/orch-verifier"

#: Sentinel used when the toolchain path does not exist or is unreadable.
TOOLCHAIN_UNAVAILABLE: str = "TOOLCHAIN_UNAVAILABLE"


class ToolchainFingerprintError(ReplayError):
    """Raised when no valid verifier-toolchain identity can be derived.

    This is the low-level failure for a missing/unreadable toolchain path or
    an absence of valid package metadata: such a toolchain has no identity,
    and a normal-looking fingerprint must never be manufactured for it.
    """


@dataclass(frozen=True)
class PackageInfo:
    """Name/version pair extracted from a .dist-info/METADATA file."""

    name: str
    version: str

    def to_dict(self) -> dict[str, str]:
        """Canonical dictionary representation."""
        return {"name": self.name, "version": self.version}


@dataclass(frozen=True)
class ToolchainFingerprint:
    """Deterministic fingerprint of the dedicated verifier toolchain.

    Stored in execution evidence — never in Project Ledger authority.
    """

    python_executable: str
    python_version: str
    toolchain_path: str
    packages: tuple[PackageInfo, ...]
    fingerprint: str  # sha256 of canonical JSON of sorted name/version pairs

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        return {
            "python_executable": self.python_executable,
            "python_version": self.python_version,
            "toolchain_path": self.toolchain_path,
            "packages": [p.to_dict() for p in self.packages],
            "fingerprint": self.fingerprint,
        }

    def matches(self, other: ToolchainFingerprint) -> bool:
        """Return True iff both fingerprints are identical."""
        return self.fingerprint == other.fingerprint


def _read_metadata_field(metadata_path: Path, field_name: str) -> str | None:
    """Extract a single RFC 822 header field from a METADATA file."""
    try:
        text = metadata_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    prefix = field_name.lower() + ":"
    for line in text.splitlines():
        if line.lower().startswith(prefix):
            return line[len(prefix):].strip()
        # RFC 822 headers end at first blank line
        if not line.strip():
            break
    return None


def _discover_packages(toolchain_path: Path) -> tuple[PackageInfo, ...]:
    """Discover installed packages from .dist-info/METADATA files.

    Reads only Name and Version fields from each METADATA file.
    No subprocess invocation, no ambient pip.
    """
    packages: list[PackageInfo] = []
    try:
        entries = sorted(toolchain_path.iterdir())
    except OSError:
        return ()

    for entry in entries:
        if not entry.is_dir():
            continue
        if not entry.name.endswith(".dist-info"):
            continue
        metadata_file = entry / "METADATA"
        if not metadata_file.exists():
            # Some dist-info dirs use PKG-INFO instead (rare)
            metadata_file = entry / "PKG-INFO"
            if not metadata_file.exists():
                continue

        name = _read_metadata_field(metadata_file, "Name")
        version = _read_metadata_field(metadata_file, "Version")
        if name and version:
            packages.append(PackageInfo(name=name.lower(), version=version))

    # Sort deterministically by (name, version)
    packages.sort(key=lambda p: (p.name, p.version))
    return tuple(packages)


def compute_toolchain_fingerprint(
    toolchain_path: str | Path | None = None,
) -> ToolchainFingerprint:
    """Compute a deterministic fingerprint of the verifier toolchain.

    Derives identity from .dist-info/METADATA files under ``toolchain_path``
    and from the Python executable and version string.

    Args:
        toolchain_path: Path to the verifier toolchain directory.
            Defaults to VERIFIER_TOOLCHAIN_PATH.

    Returns:
        ToolchainFingerprint with stable fingerprint suitable for
        cross-run comparability checks.

    Raises:
        ToolchainFingerprintError: If the toolchain path is not a readable
            directory, or no valid package metadata can be discovered.  An
            empty toolchain has no identity; a normal-looking SHA is never
            produced for absence.
    """
    resolved_path = Path(toolchain_path if toolchain_path is not None else VERIFIER_TOOLCHAIN_PATH)

    if not resolved_path.is_dir():
        raise ToolchainFingerprintError(
            f"Verifier toolchain path is not a readable directory: {resolved_path}"
        )

    # Python identity
    python_executable = "/usr/bin/python3"
    python_version = _get_python_version()

    packages = _discover_packages(resolved_path)

    if not packages:
        raise ToolchainFingerprintError(
            f"No valid package metadata discovered under verifier toolchain "
            f"path: {resolved_path}"
        )

    # Compute fingerprint: sha256 of canonical JSON of sorted name/version pairs
    fingerprint_payload = {
        "python_executable": python_executable,
        "python_version": python_version,
        "toolchain_path": str(resolved_path),
        "packages": [p.to_dict() for p in packages],
    }
    canonical = json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    return ToolchainFingerprint(
        python_executable=python_executable,
        python_version=python_version,
        toolchain_path=str(resolved_path),
        packages=packages,
        fingerprint=fingerprint,
    )


def _get_python_version() -> str:
    """Return the Python version string deterministically from the runtime."""
    import sys

    return sys.version.split()[0]  # e.g. "3.12.3"


def toolchain_fingerprints_match(a: ToolchainFingerprint, b: ToolchainFingerprint) -> bool:
    """Return True iff both fingerprints are identical (same toolchain)."""
    return a.fingerprint == b.fingerprint


def fingerprint_or_unavailable(toolchain_path: str | Path | None = None) -> str:
    """Compute fingerprint string, or return TOOLCHAIN_UNAVAILABLE sentinel.

    The sentinel is returned whenever the dedicated verifier toolchain is
    missing, unreadable, invalid, or contains an EMPTY package set.  The
    low-level compute_toolchain_fingerprint fails closed on exactly those
    conditions (ToolchainFingerprintError); this public wrapper catches it and
    yields the sentinel, so a normal-looking SHA over an empty package set is
    never produced: an empty toolchain is not an identity, it is the absence
    of one.

    Suitable for storing as a string field in MeasurementRecord evidence.
    """
    try:
        fp = compute_toolchain_fingerprint(toolchain_path)
    except ToolchainFingerprintError:
        return TOOLCHAIN_UNAVAILABLE
    return fp.fingerprint


def fingerprint_is_unavailable(value: str | None) -> bool:
    """Return True if the value denotes an absent/unknown toolchain fingerprint.

    Unavailable values are: None, empty string, the TOOLCHAIN_UNAVAILABLE
    sentinel, and the generic UNKNOWN sentinel.  Two runs whose fingerprints
    are both unavailable are never directly comparable, even when the sentinel
    strings are identical.
    """
    if value is None:
        return True
    stripped = value.strip()
    if stripped == "":
        return True
    return stripped in (TOOLCHAIN_UNAVAILABLE, UNKNOWN)
