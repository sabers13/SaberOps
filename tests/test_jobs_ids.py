"""ULID job ids (M2-3 section B)."""

from __future__ import annotations

from datetime import UTC, datetime

from saberops.jobs.ids import new_job_id

_ALPHABET = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")


def test_ulid_length_and_alphabet() -> None:
    job_id = new_job_id()
    assert len(job_id) == 26
    assert set(job_id) <= _ALPHABET


def test_ulid_time_ordering_across_milliseconds() -> None:
    first = new_job_id(
        now=datetime(2026, 10, 12, 12, 0, 0, 0, tzinfo=UTC),
        rand=lambda n: b"\xff" * n,
    )
    second = new_job_id(
        now=datetime(2026, 10, 12, 12, 0, 0, 1000, tzinfo=UTC),
        rand=lambda n: b"\x00" * n,
    )
    assert len(first) == 26 and len(second) == 26
    # Later timestamp sorts later even with all-zero randomness.
    assert first < second


def test_ulid_deterministic_with_injected_randomness() -> None:
    moment = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
    first = new_job_id(now=moment, rand=lambda n: b"\x01" * n)
    second = new_job_id(now=moment, rand=lambda n: b"\x01" * n)
    third = new_job_id(now=moment, rand=lambda n: b"\x02" * n)
    assert first == second
    assert first != third
