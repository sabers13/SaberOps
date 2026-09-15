"""Deterministic task-complexity classification for automatic routing."""

from __future__ import annotations

import re
from dataclasses import dataclass

from orchestrator_mvp.models import Tier


@dataclass(frozen=True)
class TierClassification:
    """A transparent result produced by :func:`classify_task_tier`."""

    tier: Tier
    score: int
    signals: tuple[str, ...]


def classify_task_tier(task: str) -> TierClassification:
    """Classify a task with a deterministic additive scoring table.

    The score starts at zero. Prompt length is deliberately ignored: bytes and
    requirement breadth are telemetry/planning concerns, not capability
    evidence. Add two for each *distinct* complexity keyword present
    (``refactor``, ``migrate``, ``architecture``, ``security``,
    ``performance``, ``concurrency``, ``race``, ``schema``, ``database``);
    add one for every requirement after the first, up to three, where a
    requirement is a numbered list item or a non-empty line; subtract two
    for each distinct simplifying keyword (``typo``, ``comment``,
    ``rename``, ``docs``, ``readme``, ``formatting``).  Scores at least 8 are
    T3, scores 4--7 are T2, and lower scores are T1.  Signals name every
    applied rule, making the result stable and inspectable without a model or
    network request.
    """
    normalized = task.strip().lower()
    score = 0
    signals: list[str] = []

    complexity = (
        "refactor",
        "migrate",
        "architecture",
        "security",
        "performance",
        "concurrency",
        "race",
        "schema",
        "database",
    )
    for keyword in complexity:
        if re.search(rf"\b{re.escape(keyword)}\b", normalized):
            score += 2
            signals.append(f"complexity:{keyword}")

    # Ordinary coding should start at T2, while tiny/docs work stays T1 after
    # the simplifying signals below are applied. This is intentionally one
    # small rule, not a benchmark or prompt-size scorer.
    moderate = ("implement", "implementation", "bug", "fix")
    if any(re.search(rf"\b{re.escape(keyword)}\b", normalized) for keyword in moderate):
        score += 4
        signals.append("work:ordinary-coding")

    simplifying = ("typo", "comment", "rename", "docs", "readme", "formatting")
    for keyword in simplifying:
        if re.search(rf"\b{re.escape(keyword)}\b", normalized):
            score -= 2
            signals.append(f"simplify:{keyword}")

    lines = [line for line in task.splitlines() if line.strip()]
    numbered = sum(bool(re.match(r"\s*(?:\d+[.)]|[-*])\s+", line)) for line in lines)
    requirements = numbered if numbered >= 2 else len(lines)
    extras = min(3, max(0, requirements - 1))
    if extras:
        score += extras
        signals.append(f"requirements:+{extras}")

    tier = Tier.T3 if score >= 8 else Tier.T2 if score >= 4 else Tier.T1
    return TierClassification(tier=tier, score=score, signals=tuple(signals))
