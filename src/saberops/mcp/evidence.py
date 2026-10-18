"""Allowed evidence roots and filename allowlist (ADR-0004 D7, M3).

``read_evidence`` may only read the operator evidence the engine itself
produced.  Two independent gates must both pass:

1. **Root containment** -- the fully resolved path (symlinks followed) must
   live under ``.orch/`` or ``.saberops/``.  A traversal (``../``), an absolute
   path elsewhere, a symlink that escapes, or a nested escape all resolve
   outside the roots and are refused.
2. **Filename allowlist** -- the resolved file's name must match one of the
   evidence patterns (``prompt.md``, ``report.md``, ``outcome.json``,
   ``failures.md``, ``gate.txt``, ``hygiene.md``, ``review*.md``).

A refusal is a typed :class:`PathRefusedError`; the path is never opened first
and then rejected.
"""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass
from pathlib import Path

from saberops.mcp.errors import PathRefusedError

__all__ = [
    "ALLOWED_EVIDENCE_PATTERNS",
    "MAX_EVIDENCE_BYTES",
    "EVIDENCE_ROOTS",
    "EvidenceRoots",
    "allowed_evidence_name",
    "discover_evidence_roots",
]

#: Evidence file names the MCP interface may read (ADR-0004 D7).
ALLOWED_EVIDENCE_PATTERNS: tuple[str, ...] = (
    "prompt.md",
    "report.md",
    "outcome.json",
    "failures.md",
    "gate.txt",
    "hygiene.md",
    "review*.md",
)

#: Directory names that are the only allowed evidence roots.
EVIDENCE_ROOTS: tuple[str, ...] = (".orch", ".saberops")

#: Hard cap on a single evidence read so a resource cannot stream a huge file.
MAX_EVIDENCE_BYTES = 1_048_576  # 1 MiB


def discover_evidence_roots(cwd: Path | None = None) -> tuple[Path, ...]:
    """Return the existing evidence roots, nearest first.

    Each root is resolved from an explicit environment override
    (``ORCH_DIR`` / ``SABEROPS_DIR``), else by walking up from ``cwd`` for a
    directory of that name, else ``cwd/<name>``.  Non-existent candidates are
    dropped, so a checkout without ``.saberops`` simply has one root.
    """
    base = (cwd or Path.cwd()).resolve()
    overrides = {".orch": "ORCH_DIR", ".saberops": "SABEROPS_DIR"}
    roots: list[Path] = []
    for name in EVIDENCE_ROOTS:
        override = os.environ.get(overrides[name], "").strip()
        candidate: Path | None
        if override:
            candidate = Path(override).expanduser()
        else:
            candidate = _walk_up(base, name)
        if candidate is None or not candidate.exists():
            continue
        resolved = candidate.resolve()
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def _walk_up(base: Path, name: str) -> Path | None:
    for parent in (base, *base.parents):
        candidate = parent / name
        if candidate.is_dir():
            return candidate
    return base / name


def allowed_evidence_name(name: str) -> bool:
    """Return True when ``name`` matches the evidence allowlist."""
    return any(fnmatch.fnmatch(name, pattern) for pattern in ALLOWED_EVIDENCE_PATTERNS)


@dataclass(frozen=True)
class EvidenceRoots:
    """Resolved allowed roots plus the containment/allowlist gate."""

    roots: tuple[Path, ...]

    @classmethod
    def discover(cls, cwd: Path | None = None) -> EvidenceRoots:
        return cls(roots=discover_evidence_roots(cwd))

    def resolve(self, requested: str | Path) -> Path:
        """Resolve one requested path, or raise :class:`PathRefusedError`.

        The check runs on the *resolved* path, so ``..`` segments and symlinks
        that leave a root are both caught.
        """
        if not isinstance(requested, (str, Path)):
            raise PathRefusedError("evidence path must be a string")
        raw = str(requested).strip()
        if not raw:
            raise PathRefusedError("evidence path must be a non-empty string")
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            if not self.roots:
                raise PathRefusedError("no evidence root is configured")
            candidate = self.roots[0] / candidate
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise PathRefusedError(f"evidence path cannot be resolved: {exc}") from exc
        if not self._contained(resolved):
            raise PathRefusedError(
                "evidence path escapes the allowed roots (.orch, .saberops)"
            )
        if not allowed_evidence_name(resolved.name):
            raise PathRefusedError(
                f"evidence file {resolved.name!r} is not on the allowlist"
            )
        if not resolved.is_file():
            raise PathRefusedError(f"evidence path is not a regular file: {raw}")
        return resolved

    def read(self, requested: str | Path) -> tuple[Path, str, bool]:
        """Read an allowed evidence file.

        Returns ``(resolved_path, text, truncated)``.  The text read is capped
        at :data:`MAX_EVIDENCE_BYTES`; ``truncated`` reports whether the cap was
        hit so a caller never mistakes a partial read for the whole file.
        """
        resolved = self.resolve(requested)
        try:
            size = resolved.stat().st_size
            with resolved.open("r", encoding="utf-8", errors="replace") as handle:
                text = handle.read(MAX_EVIDENCE_BYTES)
        except OSError as exc:
            raise PathRefusedError(f"evidence file cannot be read: {exc}") from exc
        return resolved, text, size > MAX_EVIDENCE_BYTES

    def list_files(self, *, limit: int = 200) -> tuple[Path, ...]:
        """List allowed evidence files under every root, deterministically."""
        found: list[Path] = []
        for root in self.roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if len(found) >= limit:
                    return tuple(found)
                if path.is_file() and allowed_evidence_name(path.name):
                    if self._contained(path.resolve()):
                        found.append(path.resolve())
        return tuple(found[:limit])

    def _contained(self, resolved: Path) -> bool:
        for root in self.roots:
            try:
                if resolved.is_relative_to(root):
                    return True
            except ValueError:  # pragma: no cover - is_relative_to never raises here
                continue
        return False
