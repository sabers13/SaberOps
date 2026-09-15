"""Normalize gateway-supplied quota observations into C07 semantics.

C07 owns quota *policy*; C10 only normalizes *observations* from a
gateway into the canonical ``remaining_pct``/``reset_at``/``freshness``
shape that C07 already consumes.

Important invariants:

* Unknown observations stay unknown; the gateway never reports
  ``0%`` or ``100%`` as a fabrication.
* The 15% STRICT red line is enforced by C07; this module only emits the
  raw numeric value.
* Shared pools are honored: multiple accounts that share a pool produce
  one canonical observation when a shared upstream capacity figure is
  provided. ``remaining_pct`` is reported per account even when the
  authoritative observation is at the pool level.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from saberops.gateway.contracts import (
    GatewayAccount,
    GatewayManifestError,
    GatewayQuotaFreshness,
    GatewayQuotaObservation,
)
from saberops.gateway.health import utc_now_iso

RED_LINE_PERCENT = 15.0


@dataclass(frozen=True)
class GatewayQuotaPoolView:
    """Normalized per-pool observation aggregated from multiple accounts."""

    pool_id: str
    provider: str
    remaining_pct: float | None
    reset_at: str | None
    freshness: GatewayQuotaFreshness
    observed_at: str
    member_account_ids: tuple[str, ...]
    raw_source: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "pool_id": self.pool_id,
            "provider": self.provider,
            "remaining_pct": self.remaining_pct,
            "reset_at": self.reset_at,
            "freshness": self.freshness.value,
            "observed_at": self.observed_at,
            "member_account_ids": list(self.member_account_ids),
            "raw_source": self.raw_source,
        }


def normalize_observation(
    *,
    account: GatewayAccount,
    remaining_pct: float | None,
    reset_at: str | None,
    freshness: GatewayQuotaFreshness,
    observed_at: str | None = None,
    raw_source: str | None = None,
) -> GatewayQuotaObservation:
    """Return a typed :class:`GatewayQuotaObservation`.

    ``remaining_pct`` may be ``None`` to indicate that the gateway could
    not determine a value; the returned observation is still usable and
    is not silently fabricated as ``0`` or ``100``.
    """
    timestamp = observed_at or utc_now_iso()
    if remaining_pct is not None and not 0.0 <= float(remaining_pct) <= 100.0:
        raise GatewayManifestError(
            f"remaining_pct must be between 0 and 100 (got {remaining_pct!r})"
        )
    return GatewayQuotaObservation(
        account_id=account.account_id,
        provider=account.provider,
        pool_id=account.pool_id,
        remaining_pct=float(remaining_pct) if remaining_pct is not None else None,
        observed_at=timestamp,
        reset_at=reset_at,
        freshness=freshness,
        raw_source=raw_source,
    )


def aggregate_pool(
    pool_id: str,
    provider: str,
    observations: list[GatewayQuotaObservation],
    *,
    observed_at: str | None = None,
) -> GatewayQuotaPoolView:
    """Aggregate per-account observations into a single pool view.

    Pool semantics: if any observation is authoritative
    (``remaining_pct is not None``), the pool view inherits it. All
    observations with ``remaining_pct is None`` collapse to an unknown
    pool view only when no authoritative observation exists.
    """
    timestamp = observed_at or utc_now_iso()
    member_ids = tuple(sorted({observation.account_id for observation in observations}))
    if not observations:
        return GatewayQuotaPoolView(
            pool_id=pool_id,
            provider=provider,
            remaining_pct=None,
            reset_at=None,
            freshness=GatewayQuotaFreshness.UNKNOWN,
            observed_at=timestamp,
            member_account_ids=member_ids,
        )
    auth = [obs for obs in observations if obs.remaining_pct is not None]
    if not auth:
        return GatewayQuotaPoolView(
            pool_id=pool_id,
            provider=provider,
            remaining_pct=None,
            reset_at=None,
            freshness=GatewayQuotaFreshness.UNKNOWN,
            observed_at=timestamp,
            member_account_ids=member_ids,
            raw_source=observations[0].raw_source,
        )
    auth_sorted = sorted(auth, key=lambda item: item.observed_at)
    remaining = min(item.remaining_pct or 0.0 for item in auth_sorted)
    reset_at = next(
        (item.reset_at for item in auth_sorted if item.reset_at is not None),
        None,
    )
    freshness = min(
        (item.freshness for item in auth_sorted),
        key=lambda value: (
            0 if value == GatewayQuotaFreshness.FRESH else 1,
        ),
    )
    return GatewayQuotaPoolView(
        pool_id=pool_id,
        provider=provider,
        remaining_pct=float(remaining),
        reset_at=reset_at,
        freshness=freshness,
        observed_at=timestamp,
        member_account_ids=member_ids,
        raw_source=auth_sorted[-1].raw_source,
    )


def is_below_red_line(
    observation: GatewayQuotaObservation | GatewayQuotaPoolView,
    *,
    red_line_pct: float = RED_LINE_PERCENT,
) -> bool | None:
    """Return ``True`` if the observation is strictly below the red line.

    Returns ``None`` when the observation is unknown. The boundary is
    exact: ``14.9`` is below, ``15.0`` is eligible.
    """
    if observation.remaining_pct is None:
        return None
    return float(observation.remaining_pct) < float(red_line_pct)


__all__ = [
    "GatewayQuotaPoolView",
    "RED_LINE_PERCENT",
    "aggregate_pool",
    "is_below_red_line",
    "normalize_observation",
]
