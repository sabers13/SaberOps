"""Small deterministic routing decisions."""

from __future__ import annotations

from dataclasses import dataclass

from saberops.config import RunConfig, cap_tier
from saberops.jobs.models import Difficulty
from saberops.models import (
    DispatchOutcome,
    DispatchStage,
    Tier,
    WorkerResult,
)


@dataclass(frozen=True)
class TierDecision:
    """Inspectable tier choice for exactly one package/stage scope."""

    stage: DispatchStage
    package_id: str | None
    base_tier: Tier
    effective_tier: Tier
    source: str
    signals: tuple[str, ...]
    max_auto_tier: Tier
    escalation_reason: str | None = None

    def as_payload(self) -> dict[str, object]:
        return {
            "stage": self.stage.value,
            "package_id": self.package_id,
            "base_tier": self.base_tier.value,
            "effective_tier": self.effective_tier.value,
            "source": self.source,
            "signals": list(self.signals),
            "max_auto_tier": self.max_auto_tier.value,
            "escalation_reason": self.escalation_reason,
        }


def choose_tier(
    config: RunConfig,
    *,
    stage: DispatchStage,
) -> TierDecision:
    """Choose a fresh base tier from the planner-recorded difficulty.

    The planner's judgement is the only input.  ``hard`` is the strong tier,
    ``routine`` (or a missing value) is the ordinary one, and the owner's
    ``max_auto_tier`` ceiling still caps both.  No keyword, prompt length or
    prior run tier participates.
    """
    package_id = None
    if config.routing_mode == "manual":
        assert config.manual_tier is not None
        return TierDecision(
            stage=stage,
            package_id=package_id,
            base_tier=config.manual_tier,
            effective_tier=config.manual_tier,
            source="manual_exact",
            signals=("manual_tier",),
            max_auto_tier=config.max_auto_tier,
        )

    base = Tier.T3 if config.difficulty is Difficulty.HARD else Tier.T1
    effective = cap_tier(base, config.max_auto_tier)
    signals: tuple[str, ...] = (f"difficulty:{config.difficulty.value}",)
    if effective != base:
        signals = (*signals, f"ceiling:{config.max_auto_tier.value}")
    return TierDecision(
        stage=stage,
        package_id=package_id,
        base_tier=base,
        effective_tier=effective,
        source=f"{stage.value}_difficulty",
        signals=signals,
        max_auto_tier=config.max_auto_tier,
    )


def escalated_decision(
    decision: TierDecision,
    tier: Tier,
    reason: DispatchOutcome,
    *,
    stage: DispatchStage | None = None,
) -> TierDecision:
    """Return the same local decision advanced for explicit typed evidence."""
    return TierDecision(
        stage=stage or decision.stage,
        package_id=decision.package_id,
        base_tier=decision.base_tier,
        effective_tier=tier,
        source=decision.source,
        signals=decision.signals,
        max_auto_tier=decision.max_auto_tier,
        escalation_reason=reason.value,
    )


def classify_worker_outcome(result: WorkerResult) -> DispatchOutcome:
    """Classify a completed worker call without interpreting free-form text."""
    if result.status.value == "TIMEOUT":
        return DispatchOutcome.TIMEOUT
    if result.outcome is not None:
        return result.outcome
    # A bare FAILED result has no structured evidence of either provider
    # failure or capability failure. Unknown is intentionally conservative.
    return DispatchOutcome.UNKNOWN


def is_escalation_evidence(outcome: DispatchOutcome) -> bool:
    """Only an explicit typed capability failure justifies cross-tier spend.

    A gate failure alone is insufficient evidence to buy a stronger
    tier -- process evidence cannot distinguish a misconfigured gate from a
    genuine candidate validation failure -- so ``GATE_FAILURE`` never
    authorizes escalation.  Gate truth is still recorded durably; only its
    spend authority is removed.
    """
    return outcome is DispatchOutcome.CAPABILITY_FAILURE
