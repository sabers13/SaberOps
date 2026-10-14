"""M2-4a cost-model tests: buckets, priors, inference, EWMA."""

from __future__ import annotations

import pytest

from saberops.capacity.cost_model import (
    ALPHA,
    MIN_SAMPLES,
    CostState,
    SizeBucket,
    cost_estimate,
    infer_bucket,
    prior_pct,
    update,
    validate_route,
)


def test_size_bucket_values() -> None:
    assert [b.value for b in SizeBucket] == ["S", "M", "L"]


def test_priors_per_window() -> None:
    assert prior_pct("5h", SizeBucket.S) == 3.0
    assert prior_pct("5h", SizeBucket.M) == 8.0
    assert prior_pct("5h", SizeBucket.L) == 15.0
    assert prior_pct("session", SizeBucket.S) == 3.0
    assert prior_pct("session", SizeBucket.M) == 8.0
    assert prior_pct("session", SizeBucket.L) == 15.0
    assert prior_pct("daily", SizeBucket.S) == 2.0
    assert prior_pct("daily", SizeBucket.M) == 5.0
    assert prior_pct("daily", SizeBucket.L) == 10.0
    assert prior_pct("weekly", SizeBucket.S) == 1.0
    assert prior_pct("weekly", SizeBucket.M) == 2.5
    assert prior_pct("weekly", SizeBucket.L) == 5.0
    assert prior_pct("app-weekly", SizeBucket.S) == 1.0
    assert prior_pct("app-weekly", SizeBucket.M) == 2.5
    assert prior_pct("app-weekly", SizeBucket.L) == 5.0
    assert prior_pct("monthly", SizeBucket.S) == 0.3
    assert prior_pct("monthly", SizeBucket.M) == 0.8
    assert prior_pct("monthly", SizeBucket.L) == 1.5


def test_priors_fallback_for_unknown_windows() -> None:
    for window in ("model:x", "ALL", "all", "bogus", ""):
        assert prior_pct(window, SizeBucket.S) == 3.0
        assert prior_pct(window, SizeBucket.M) == 8.0
        assert prior_pct(window, SizeBucket.L) == 15.0


def test_infer_bucket_boundaries() -> None:
    assert infer_bucket(8_191, 1) is SizeBucket.S
    assert infer_bucket(8_192, 1) is SizeBucket.M
    assert infer_bucket(65_535, 1) is SizeBucket.M
    assert infer_bucket(65_536, 1) is SizeBucket.L
    assert infer_bucket(100, 1) is SizeBucket.S
    assert infer_bucket(100, 2) is SizeBucket.M
    assert infer_bucket(100, 3) is SizeBucket.M
    assert infer_bucket(100, 4) is SizeBucket.L
    assert infer_bucket(65_536, 4) is SizeBucket.L


def test_ewma_updates() -> None:
    assert ALPHA == 0.3
    assert MIN_SAMPLES == 3
    s1 = update(CostState(samples=0, ewma_pct=None), 10.0)
    assert (s1.samples, s1.ewma_pct) == (1, 10.0)
    s2 = update(s1, 20.0)
    assert s2.samples == 2
    assert s2.ewma_pct == pytest.approx(0.3 * 20.0 + 0.7 * 10.0)  # 13.0
    s3 = update(s2, 30.0)
    assert s3.samples == 3
    assert s3.ewma_pct == pytest.approx(0.3 * 30.0 + 0.7 * 13.0)  # 18.1
    s4 = update(s3, 40.0)
    assert s4.samples == 4
    assert s4.ewma_pct == pytest.approx(0.3 * 40.0 + 0.7 * 18.1)  # 24.67


def test_prior_below_min_samples_then_ewma() -> None:
    s1 = update(None, 10.0)
    est1 = cost_estimate(s1, "5h", SizeBucket.S)
    assert est1.learned is False
    assert est1.pct == pytest.approx(3.0)
    assert est1.spread == pytest.approx(0.5 * 3.0)
    s2 = update(s1, 20.0)
    est2 = cost_estimate(s2, "5h", SizeBucket.S)
    assert est2.learned is False
    assert est2.pct == pytest.approx(3.0)
    s3 = update(s2, 30.0)
    est3 = cost_estimate(s3, "5h", SizeBucket.S)
    assert est3.learned is True
    assert est3.pct == pytest.approx(18.1)
    assert est3.spread == pytest.approx(0.25 * 18.1)
    est_none = cost_estimate(None, "weekly", SizeBucket.M)
    assert est_none.learned is False
    assert est_none.pct == pytest.approx(2.5)
    assert est_none.spread == pytest.approx(0.5 * 2.5)


def test_negative_sample_rejected() -> None:
    with pytest.raises(ValueError):
        update(CostState(samples=1, ewma_pct=5.0), -1.0)


def test_empty_route_rejected() -> None:
    with pytest.raises(ValueError):
        validate_route("")
    with pytest.raises(ValueError):
        validate_route("   ")
    with pytest.raises(ValueError):
        validate_route("codex")
    with pytest.raises(ValueError):
        validate_route("codex/")
    with pytest.raises(ValueError):
        validate_route("/gpt-5")
    assert validate_route("codex/gpt-5") == "codex/gpt-5"
