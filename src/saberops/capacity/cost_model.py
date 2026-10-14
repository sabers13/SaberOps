"""Learned per-job cost model (M2-4a B).

Pure, no I/O. One cost state per ``(route, pool_id, window_id, bucket)``
key, where ``route`` is the routing-chain candidate id ``provider/model``.
States are persisted by :mod:`saberops.capacity.ledger` in the
``capacity_cost_model`` owner-DB table; this module only computes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

ALPHA = 0.3
MIN_SAMPLES = 3

PRIOR_SPREAD_FRACTION = 0.5
LEARNED_SPREAD_FRACTION = 0.25


class SizeBucket(StrEnum):
    S = "S"
    M = "M"
    L = "L"


# Packaged placeholder priors: percent of the window per job, keyed by
# (window_id.lower(), bucket). Owner-configured priors arrive with M2-4b.
_PRIORS: dict[tuple[str, str], float] = {
    ("5h", "S"): 3.0,
    ("5h", "M"): 8.0,
    ("5h", "L"): 15.0,
    ("session", "S"): 3.0,
    ("session", "M"): 8.0,
    ("session", "L"): 15.0,
    ("daily", "S"): 2.0,
    ("daily", "M"): 5.0,
    ("daily", "L"): 10.0,
    ("weekly", "S"): 1.0,
    ("weekly", "M"): 2.5,
    ("weekly", "L"): 5.0,
    ("app-weekly", "S"): 1.0,
    ("app-weekly", "M"): 2.5,
    ("app-weekly", "L"): 5.0,
    ("monthly", "S"): 0.3,
    ("monthly", "M"): 0.8,
    ("monthly", "L"): 1.5,
}

_FALLBACK_WINDOW = "5h"


def _coerce_bucket(bucket: SizeBucket | str) -> SizeBucket:
    if isinstance(bucket, SizeBucket):
        return bucket
    try:
        return SizeBucket(str(bucket))
    except ValueError as exc:
        raise ValueError(f"unknown size bucket: {bucket!r}") from exc


def prior_pct(window_id: str, bucket: SizeBucket | str) -> float:
    """Return the packaged prior percent for a window/bucket.

    Any window id outside the known table (``model:*``, ``ALL``,
    unknown) falls back to the ``5h`` values (conservative).
    """
    b = _coerce_bucket(bucket)
    key = ((window_id or "").strip().lower(), b.value)
    if key in _PRIORS:
        return _PRIORS[key]
    return _PRIORS[(_FALLBACK_WINDOW, b.value)]


def infer_bucket(prompt_bytes: int, package_count: int) -> SizeBucket:
    """Infer a size bucket when the job has no ``size_hint``.

    Called at dispatch only (by M2-4b); nothing calls it in this lane
    except tests.
    """
    if prompt_bytes < 8_192 and package_count <= 1:
        return SizeBucket.S
    if prompt_bytes >= 65_536 or package_count >= 4:
        return SizeBucket.L
    return SizeBucket.M


@dataclass(frozen=True)
class CostState:
    samples: int
    ewma_pct: float | None


@dataclass(frozen=True)
class CostEstimate:
    pct: float
    spread: float
    learned: bool


def update(state: CostState | None, sample_pct: float) -> CostState:
    """Fold one observed percent sample into a cost state (EWMA)."""
    sample = float(sample_pct)
    if sample < 0:
        raise ValueError("cost sample must be non-negative")
    samples = int(state.samples) if state is not None else 0
    previous = state.ewma_pct if state is not None else None
    if previous is None or samples <= 0:
        return CostState(samples=samples + 1, ewma_pct=sample)
    ewma = ALPHA * sample + (1.0 - ALPHA) * float(previous)
    return CostState(samples=samples + 1, ewma_pct=ewma)


def cost_estimate(
    state: CostState | None,
    window_id: str,
    bucket: SizeBucket | str,
) -> CostEstimate:
    """Return the cost estimate for a key.

    Below ``MIN_SAMPLES`` the packaged prior applies with a 50 % spread
    (``learned=False``); from the 3rd sample the EWMA applies with a
    25 % spread (``learned=True``).
    """
    b = _coerce_bucket(bucket)
    samples = int(state.samples) if state is not None else 0
    if state is not None and state.ewma_pct is not None and samples >= MIN_SAMPLES:
        ewma = float(state.ewma_pct)
        return CostEstimate(
            pct=ewma,
            spread=LEARNED_SPREAD_FRACTION * ewma,
            learned=True,
        )
    prior = prior_pct(window_id, b)
    return CostEstimate(
        pct=prior,
        spread=PRIOR_SPREAD_FRACTION * prior,
        learned=False,
    )


def validate_route(route: str) -> str:
    """Validate a cost-model route key (``provider/model``), returning it.

    The key is never a bare provider: it must contain a slash separating
    non-empty provider and model parts. Empty routes or bare providers
    are rejected with ``ValueError``.
    """
    text = route.strip() if isinstance(route, str) else ""
    if not text or "/" not in text:
        raise ValueError("route must be a non-empty provider/model id")
    provider, _, model = text.partition("/")
    if not provider.strip() or not model.strip():
        raise ValueError("route must be a non-empty provider/model id")
    return text


__all__ = [
    "ALPHA",
    "MIN_SAMPLES",
    "PRIOR_SPREAD_FRACTION",
    "LEARNED_SPREAD_FRACTION",
    "SizeBucket",
    "CostState",
    "CostEstimate",
    "prior_pct",
    "infer_bucket",
    "update",
    "cost_estimate",
    "validate_route",
]
