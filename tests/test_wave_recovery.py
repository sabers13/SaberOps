"""Validation of the retained wave model record."""

from datetime import UTC, datetime

import pytest

from saberops.wave_models import LanePreemption, WaveModelError


def _sha(index: int) -> str:
    return f"{index:040x}"


def test_lane_preemption_record_validates_and_round_trips() -> None:
    """Covers: WX-2 / M2-5 (the park record is validated and serializable)."""
    record = LanePreemption(
        lane_id="L1",
        checkpoint_ref="refs/saberops/checkpoints/job-1/1",
        checkpoint_sha=_sha(7),
        reason="quota_exhausted_no_admitted_fallback",
        preemptions=1,
        job_id="job-1",
        reset_deadline="2026-10-18T12:00:00+00:00",
        not_before="2026-10-18T12:00:00+00:00",
    )
    assert LanePreemption.from_dict(record.as_dict()) == record
    assert record.exclusion_expired(datetime(2026, 10, 18, 11, 0, tzinfo=UTC)) is False
    assert record.exclusion_expired(datetime(2026, 10, 18, 13, 0, tzinfo=UTC)) is True
    with pytest.raises(WaveModelError):
        LanePreemption(
            lane_id="L1",
            checkpoint_ref="ref",
            checkpoint_sha="A" * 40,
            reason="quota",
        )
    with pytest.raises(WaveModelError):
        LanePreemption(
            lane_id="L1",
            checkpoint_ref="ref",
            checkpoint_sha=_sha(1),
            reason="quota",
            not_before="2026-10-18T12:00:00",
        )
    with pytest.raises(WaveModelError):
        record.exclusion_expired(datetime(2026, 10, 18, 12, 0))
    with pytest.raises(WaveModelError):
        LanePreemption.from_dict({"lane_id": "L1"})
