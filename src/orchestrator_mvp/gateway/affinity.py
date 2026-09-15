"""Bounded gateway account affinity.

Account affinity is a *preference*, not authority. The gateway may
retain an equivalent account across dispatches inside the same run while
that account remains healthy, quota-eligible, policy-eligible, and
compatible with the exact provider/model. Affinity is broken by a
single typed reason and the break evidence is always recorded.

Affinity is NOT provider/session continuation (that is C05's
responsibility). It is a pure infrastructure hint scoped to one run.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass

from orchestrator_mvp.gateway.contracts import (
    GatewayAccount,
    GatewayHealth,
    GatewayManifestError,
    GatewayQuotaObservation,
)
from orchestrator_mvp.gateway.quota import GatewayQuotaPoolView


@dataclass(frozen=True)
class AffinityHint:
    """A bounded affinity hint scoped to one (provider, model) target.

    The ``provider`` and ``model`` here are the *semantic* target that
    was already selected; the affinity is only retained for an account
    that matches it.
    """

    run_id: str
    provider: str
    model: str
    account_id: str
    created_at: str

    def __post_init__(self) -> None:
        if not self.run_id.strip():
            raise GatewayManifestError("affinity hint run_id must be non-empty")
        if not self.provider.strip() or not self.model.strip():
            raise GatewayManifestError("affinity hint provider/model must be non-empty")
        if not self.account_id.strip():
            raise GatewayManifestError("affinity hint account_id must be non-empty")
        if not self.created_at.strip():
            raise GatewayManifestError("affinity hint created_at must be non-empty")

    def matches(self, provider: str, model: str) -> bool:
        return (
            provider.strip().lower() == self.provider.strip().lower()
            and model.strip().lower() == self.model.strip().lower()
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "provider": self.provider,
            "model": self.model,
            "account_id": self.account_id,
            "created_at": self.created_at,
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class AffinityBreakEvidence:
    """Recorded evidence explaining why an affinity hint was broken."""

    hint: AffinityHint
    reason: str
    old_account_id: str
    new_account_id: str | None
    recorded_at: str

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise GatewayManifestError("affinity break reason must be non-empty")
        if not self.recorded_at.strip():
            raise GatewayManifestError("affinity break recorded_at must be non-empty")

    def as_dict(self) -> dict[str, object]:
        return {
            "hint": self.hint.as_dict(),
            "reason": self.reason,
            "old_account_id": self.old_account_id,
            "new_account_id": self.new_account_id,
            "recorded_at": self.recorded_at,
        }


def matches_target(hint: AffinityHint, provider: str, model: str) -> bool:
    return hint.matches(provider, model)


def should_break(
    hint: AffinityHint,
    *,
    accounts: Iterable[GatewayAccount],
    health: GatewayHealth | None,
    quota: GatewayQuotaObservation | GatewayQuotaPoolView | None,
    red_line_pct: float,
) -> str | None:
    """Return a typed break reason if the hint can no longer be honored.

    Returns ``None`` when the hint may be retained. Reasons are stable
    strings that may be persisted and asserted by tests.
    """
    candidates = [
        account
        for account in accounts
        if account.account_id == hint.account_id
    ]
    if not candidates:
        return "ACCOUNT_NO_LONGER_CONFIGURED"
    account = candidates[0]
    if account.provider.strip().lower() != hint.provider.strip().lower():
        return "PROVIDER_MISMATCH"
    if health is not None:
        if health.status.value == "UNAVAILABLE":
            return "ACCOUNT_UNAVAILABLE"
        if health.status.value == "COOLDOWN" and health.cooldown_until:
            # Caller passes now via health observed_at; the affinity layer
            # is purely evidence - the caller enforces the timestamp
            # comparison. We return the typed reason and let dispatch
            # verify the window.
            return "ACCOUNT_IN_COOLDOWN"
    if quota is not None and quota.remaining_pct is not None:
        if float(quota.remaining_pct) < float(red_line_pct):
            return "QUOTA_BELOW_RED_LINE"
    return None


def record_break(
    hint: AffinityHint,
    *,
    reason: str,
    new_account_id: str | None,
    recorded_at: str,
) -> AffinityBreakEvidence:
    """Record the break evidence for persistence."""
    if not reason.strip():
        raise GatewayManifestError("affinity break reason must be non-empty")
    if new_account_id is not None and not new_account_id.strip():
        raise GatewayManifestError("new_account_id must be None or non-empty")
    return AffinityBreakEvidence(
        hint=hint,
        reason=reason,
        old_account_id=hint.account_id,
        new_account_id=new_account_id,
        recorded_at=recorded_at,
    )


__all__ = [
    "AffinityBreakEvidence",
    "AffinityHint",
    "matches_target",
    "record_break",
    "should_break",
]
