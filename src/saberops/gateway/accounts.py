"""Account selection under the C10 invariant.

The selection pipeline is intentionally narrow:

1. Restrict the configured account set to those that match the exact
   already-selected ``provider`` and ``model``. The gateway MUST NOT
   substitute a different provider/model/tier/role.
2. Drop terminal/unusable accounts (no auth, disabled, unavailable).
3. Filter by current health/cooldown.
4. Apply C07 quota eligibility at the account or shared-pool level.
5. Optionally retain a healthy affinity hint.
6. Otherwise rank by infrastructure preference (free capacity is a tie
   breaker, never a substitute for semantic eligibility).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from saberops.gateway.contracts import (
    GatewayAccount,
    GatewayHealth,
    GatewayManifestError,
    GatewayQuotaFreshness,
    GatewayQuotaObservation,
)
from saberops.gateway.health import GatewayHealthState
from saberops.gateway.quota import GatewayQuotaPoolView


@dataclass(frozen=True)
class AccountSelectionInputs:
    """Inputs to :func:`select_account`.

    ``now`` is the timestamp used for cooldown/reset window comparisons.
    ``quota_observations`` may contain one observation per account OR a
    single observation per ``pool_id``; the matching rule picks the
    closest match.
    """

    candidate_provider: str
    candidate_model: str
    accounts: Sequence[GatewayAccount]
    health: GatewayHealthState
    quota_observations: tuple[GatewayQuotaObservation | GatewayQuotaPoolView, ...]
    pool_observations: tuple[GatewayQuotaPoolView, ...]
    affinity_account_id: str | None
    now: str
    red_line_pct: float = 15.0


@dataclass(frozen=True)
class AccountSelectionResult:
    """The output of :func:`select_account`."""

    selected: GatewayAccount
    affinity_retained: bool
    affinity_broken: bool
    affinity_break_reason: str | None
    selection_reason: str
    considered: tuple[GatewayAccount, ...]
    health: GatewayHealth | None
    quota: GatewayQuotaObservation | GatewayQuotaPoolView | None


def _eligible_providers_models(
    accounts: Iterable[GatewayAccount], provider: str, model: str
) -> list[GatewayAccount]:
    normalized_provider = provider.strip().lower()
    return [
        account
        for account in accounts
        if account.provider.strip().lower() == normalized_provider
    ]


def _drop_terminal(
    accounts: Iterable[GatewayAccount], inputs: AccountSelectionInputs
) -> list[GatewayAccount]:
    """Drop accounts whose terminal state (health) makes them unusable."""
    return [
        account
        for account in accounts
        if inputs.health.is_callable(account, now=inputs.now)
    ]


def _matching_quota(
    account: GatewayAccount,
    inputs: AccountSelectionInputs,
) -> GatewayQuotaObservation | GatewayQuotaPoolView | None:
    if account.pool_id is not None:
        for view in inputs.pool_observations:
            if view.pool_id == account.pool_id:
                return view
    for observation in inputs.quota_observations:
        if isinstance(observation, GatewayQuotaObservation):
            if observation.account_id == account.account_id:
                return observation
    return None


def _quota_blocks(
    account: GatewayAccount,
    quota: GatewayQuotaObservation | GatewayQuotaPoolView | None,
    red_line_pct: float,
) -> bool:
    """Decide whether a quota observation excludes ``account``.

    Unknown and stale observations never block (mirrors C07 quota
    semantics: the C10 gateway must not pretend to know more than its
    sources reported). The boundary is exact: a value strictly below
    ``red_line_pct`` blocks; the red line itself is eligible.
    """
    if quota is None or quota.remaining_pct is None:
        return False
    freshness = getattr(quota, "freshness", None)
    if freshness is not None and freshness != GatewayQuotaFreshness.FRESH:
        return False
    return float(quota.remaining_pct) < float(red_line_pct)


def _apply_quota(
    accounts: Iterable[GatewayAccount], inputs: AccountSelectionInputs
) -> list[GatewayAccount]:
    survivors: list[GatewayAccount] = []
    for account in accounts:
        quota = _matching_quota(account, inputs)
        if _quota_blocks(account, quota, inputs.red_line_pct):
            continue
        survivors.append(account)
    return survivors


def _rank(
    accounts: Iterable[GatewayAccount], inputs: AccountSelectionInputs
) -> list[GatewayAccount]:
    """Sort by ``free_capacity`` (cheap first) and a stable tiebreaker."""
    return sorted(
        accounts,
        key=lambda account: (
            0 if account.free_capacity else 1,
            account.canonical_id,
        ),
    )


def select_account(inputs: AccountSelectionInputs) -> AccountSelectionResult:
    """Choose one equivalent account/connection for ``candidate``."""
    if not inputs.accounts:
        raise GatewayManifestError("select_account requires at least one account")
    candidates = _eligible_providers_models(
        inputs.accounts, inputs.candidate_provider, inputs.candidate_model
    )
    if not candidates:
        raise GatewayManifestError(
            "no gateway account matches the selected provider/model; "
            "this is a gateway configuration defect, not a routing decision"
        )
    considered = tuple(candidates)
    after_health = _drop_terminal(candidates, inputs)
    if not after_health:
        raise GatewayManifestError(
            "every gateway account for the selected provider/model is in cooldown/unavailable"
        )
    after_quota = _apply_quota(after_health, inputs)
    if not after_quota:
        raise GatewayManifestError(
            "every eligible gateway account is below the configured quota red line"
        )
    affinity_account: GatewayAccount | None = None
    if inputs.affinity_account_id:
        for account in after_quota:
            if account.account_id == inputs.affinity_account_id:
                affinity_account = account
                break
    ranked = _rank(after_quota, inputs)
    if affinity_account is not None:
        chosen = affinity_account
        result = AccountSelectionResult(
            selected=chosen,
            affinity_retained=True,
            affinity_broken=False,
            affinity_break_reason=None,
            selection_reason="affinity_retained_healthy_eligible",
            considered=considered,
            health=inputs.health.get(chosen.account_id),
            quota=_matching_quota(chosen, inputs),
        )
        return result
    chosen = ranked[0]
    selection_reason = (
        "first_free_capacity_ranked_eligible"
        if chosen.free_capacity
        else "first_eligible_account"
    )
    return AccountSelectionResult(
        selected=chosen,
        affinity_retained=False,
        affinity_broken=False,
        affinity_break_reason=None,
        selection_reason=selection_reason,
        considered=considered,
        health=inputs.health.get(chosen.account_id),
        quota=_matching_quota(chosen, inputs),
    )


def break_affinity(
    result: AccountSelectionResult, *, reason: str
) -> AccountSelectionResult:
    """Return a copy of ``result`` with affinity marked as broken."""
    if not reason.strip():
        raise GatewayManifestError("affinity break reason must be non-empty")
    return AccountSelectionResult(
        selected=result.selected,
        affinity_retained=False,
        affinity_broken=True,
        affinity_break_reason=reason,
        selection_reason=result.selection_reason,
        considered=result.considered,
        health=result.health,
        quota=result.quota,
    )


__all__ = [
    "AccountSelectionInputs",
    "AccountSelectionResult",
    "break_affinity",
    "select_account",
]
