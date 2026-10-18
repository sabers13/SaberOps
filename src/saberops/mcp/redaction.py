"""Secret redaction for every MCP read surface (ADR-0004 D7).

ADR-0004 D7: "Read tools never return credentials or secret references'
values."  A thin adapter cannot rely on the underlying service to be
secret-free (a job goal, a repo path, a question answer or an evidence file is
free text supplied by a human), so every value that leaves the MCP server is
passed through :func:`redact_secrets` exactly once.

Two independent rules apply:

* **Key rule** -- a mapping key whose normalized form contains a secret marker
  (``token``, ``secret``, ``api_key``, ``credential`` ...) has its value
  replaced wholesale, whatever the value type is.
* **Value rule** -- a string value that matches a known credential shape
  (``sk-...``, ``ghp_...``, ``Bearer ...``, a PEM private key header, ...) has
  the matched span replaced.

The function is total, depth-bounded and non-mutating: it always returns a new
JSON-compatible structure and never raises on unexpected input.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["REDACTED", "SECRET_KEY_MARKERS", "is_secret_key", "redact_secrets"]

REDACTED = "[REDACTED]"

#: Normalized substrings that mark a mapping key as carrying a secret value.
SECRET_KEY_MARKERS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "apikey",
    "credential",
    "authorization",
    "accesskey",
    "privatekey",
    "bearer",
    "sessionkey",
    "cookie",
    "signature",
)

#: Bounded depth so a pathological payload cannot recurse forever.
_MAX_DEPTH = 24

_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{16,}"),
    re.compile(r"gho_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

_KEY_NORMALIZER = re.compile(r"[^a-z0-9]")


def is_secret_key(key: str) -> bool:
    """Return True when ``key`` names something that must never be echoed."""
    normalized = _KEY_NORMALIZER.sub("", key.lower())
    if not normalized:
        return False
    return any(marker in normalized for marker in SECRET_KEY_MARKERS)


def _redact_value_string(value: str) -> str:
    redacted = value
    for pattern in _VALUE_PATTERNS:
        redacted = pattern.sub(REDACTED, redacted)
    return redacted


def redact_secrets(value: Any, *, _depth: int = 0) -> Any:
    """Return a redacted, JSON-compatible copy of ``value``.

    Mappings keep their keys; a secret-named key's value becomes
    :data:`REDACTED`.  Sequences keep their order.  Scalars pass through
    unchanged except strings, which lose any embedded credential span.
    """
    if _depth > _MAX_DEPTH:
        return REDACTED
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if is_secret_key(name):
                out[name] = REDACTED
            else:
                out[name] = redact_secrets(item, _depth=_depth + 1)
        return out
    if isinstance(value, (str, bytes, bytearray)):
        if isinstance(value, (bytes, bytearray)):
            try:
                return _redact_value_string(bytes(value).decode("utf-8", "replace"))
            except Exception:  # pragma: no cover - decode with replace cannot raise
                return REDACTED
        return _redact_value_string(value)
    if isinstance(value, Sequence):
        return [redact_secrets(item, _depth=_depth + 1) for item in value]
    return value
