"""Small deterministic routing decisions with stage/package-local scope."""

from __future__ import annotations

from dataclasses import dataclass

from saberops.classifier import TierClassification, classify_task_tier
from saberops.config import RunConfig, cap_tier
from saberops.models import (
    DispatchOutcome,
    DispatchStage,
    Tier,
    WorkerResult,
    WorkPackage,
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


def package_requirements(package: WorkPackage) -> str:
    """Return deterministic capability requirements for one package only."""
    parts = [package.goal, *package.acceptance_criteria, *package.constraints]
    return "\n".join(part.strip() for part in parts if part.strip())


def choose_tier(
    config: RunConfig,
    *,
    stage: DispatchStage,
    package: WorkPackage | None = None,
    requirements: str | None = None,
) -> TierDecision:
    """Choose a fresh base tier without consulting any prior/current run tier."""
    package_id = package.id if package is not None else None
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

    text = (
        requirements
        if requirements is not None
        else package_requirements(package)
        if package is not None
        else config.task
    )
    classified: TierClassification = classify_task_tier(text)
    effective = cap_tier(classified.tier, config.max_auto_tier)
    signals = classified.signals
    if effective != classified.tier:
        signals = (*signals, f"ceiling:{config.max_auto_tier.value}")
    return TierDecision(
        stage=stage,
        package_id=package_id,
        base_tier=classified.tier,
        effective_tier=effective,
        source="package_classifier" if package is not None else f"{stage.value}_classifier",
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
    """Only explicit capability and substantive gate failures justify spend."""
    return outcome in (DispatchOutcome.CAPABILITY_FAILURE, DispatchOutcome.GATE_FAILURE)
