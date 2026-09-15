"""Per-account gateway health and cooldown tracking.

The gateway layer keeps a small in-memory map of live ``GatewayHealth``
observations per account. Cooldown is enforced with a strict timestamp
comparison against an injectable clock; the helper is deterministic so
tests can freeze the clock without monkey-patching ``time``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from orchestrator_mvp.gateway.contracts import (
    GatewayAccount,
    GatewayHealth,
    GatewayManifestError,
    GatewayStatus,
)


def utc_now_iso() -> str:
    """Return the current UTC time in a stable ISO-8601 string format."""
    seconds = time.time()
    gm = time.gmtime(seconds)
    return f"{gm[0]:04d}-{gm[1]:02d}-{gm[2]:02d}T{gm[3]:02d}:{gm[4]:02d}:{gm[5]:02d}Z"


@dataclass
class GatewayHealthState:
    """Mutable in-process store mapping account_id -> last observation."""

    observations: dict[str, GatewayHealth] | None = None

    def __post_init__(self) -> None:
        if self.observations is None:
            self.observations = {}
        elif not isinstance(self.observations, dict):
            raise GatewayManifestError("health observations must be a dict")

    @classmethod
    def empty(cls) -> GatewayHealthState:
        return cls()

    def record(self, health: GatewayHealth) -> None:
        assert self.observations is not None
        self.observations[health.account_id] = health

    def get(self, account_id: str) -> GatewayHealth | None:
        assert self.observations is not None
        return self.observations.get(account_id)

    def is_callable(self, account: GatewayAccount, *, now: str) -> bool:
        """Return True iff ``account`` has no live cooldown or failure."""
        observation = self.get(account.account_id)
        if observation is None:
            return True
        if observation.status == GatewayStatus.UNAVAILABLE:
            return False
        if observation.status == GatewayStatus.COOLDOWN and observation.cooldown_until:
            if observation.cooldown_until > now:
                return False
        return observation.status in {
            GatewayStatus.HEALTHY,
            GatewayStatus.DEGRADED,
            GatewayStatus.UNKNOWN,
        }


def _compute_cooldown_until(start_iso: str, cooldown_seconds: float) -> str:
    parsed = time.strptime(start_iso, "%Y-%m-%dT%H:%M:%SZ")
    target = time.mktime(parsed) + cooldown_seconds
    gm = time.gmtime(target)
    return f"{gm[0]:04d}-{gm[1]:02d}-{gm[2]:02d}T{gm[3]:02d}:{gm[4]:02d}:{gm[5]:02d}Z"


def mark_health(
    state: GatewayHealthState,
    account: GatewayAccount,
    *,
    status: GatewayStatus,
    reason: str | None = None,
    cooldown_seconds: float | None = None,
    now: str | None = None,
    clock: Callable[[], str] = utc_now_iso,
) -> GatewayHealth:
    """Return a fresh health observation, mutating ``state`` in place."""
    if cooldown_seconds is not None and cooldown_seconds < 0:
        raise GatewayManifestError("cooldown_seconds must be non-negative")
    timestamp = now or clock()
    cooldown_until: str | None = None
    if status == GatewayStatus.COOLDOWN and cooldown_seconds:
        cooldown_until = _compute_cooldown_until(timestamp, cooldown_seconds)
    observation = GatewayHealth(
        account_id=account.account_id,
        status=status,
        observed_at=timestamp,
        cooldown_until=cooldown_until,
        reason=reason,
    )
    state.record(observation)
    return observation


def healthy_accounts(
    accounts: Iterable[GatewayAccount],
    state: GatewayHealthState,
    *,
    now: str,
) -> list[GatewayAccount]:
    """Return the subset of accounts whose current health is callable."""
    return [account for account in accounts if state.is_callable(account, now=now)]


__all__ = [
    "GatewayHealthState",
    "healthy_accounts",
    "mark_health",
    "utc_now_iso",
]
