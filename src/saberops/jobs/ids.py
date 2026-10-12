"""ULID job ids (M2-3 section B): in-house, no dependency."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_job_id(
    now: datetime | None = None, rand: Callable[[int], bytes] = os.urandom
) -> str:
    """Return a 26-char Crockford-base32 ULID.

    48-bit millisecond timestamp plus 80 random bits, so
    lexicographic order follows creation time across milliseconds.
    ``now`` and ``rand`` are injectable for tests.
    """
    if now is None:
        moment = datetime.now(UTC)
    elif now.tzinfo is None:
        moment = now.replace(tzinfo=UTC)
    else:
        moment = now.astimezone(UTC)
    millis = int(moment.timestamp() * 1000) & 0xFFFFFFFFFFFF
    raw = rand(10)
    if len(raw) != 10:
        raise ValueError(f"rand(10) must return 10 bytes, got {len(raw)}")
    rand_int = int.from_bytes(raw, "big")
    value = (millis << 80) | rand_int
    chars: list[str] = []
    for shift in range(125, -1, -5):
        chars.append(_CROCKFORD[(value >> shift) & 31])
    return "".join(chars)


__all__ = ["new_job_id"]
