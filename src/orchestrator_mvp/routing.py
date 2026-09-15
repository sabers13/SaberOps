"""Routing policy and multi-provider candidate resolution with training policy enforcement."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from orchestrator_mvp.models import Tier, WorkerCandidate
from orchestrator_mvp.routing_config import requires_training_permission

# Canonical worker candidates
CANDIDATE_ANTIGRAVITY_FLASH = WorkerCandidate(provider="antigravity", model="gemini-3.7-flash")
CANDIDATE_OPENCODE_MIMO_V2_5 = WorkerCandidate(provider="opencode", model="opencode-go/mimo-v2.5")
CANDIDATE_OPENCODE_GO_DEEPSEEK_V4_FLASH = WorkerCandidate(
    provider="opencode", model="opencode-go/deepseek-v4-flash"
)
CANDIDATE_OPENCODE_GO_DEEPSEEK_V4_PRO = WorkerCandidate(
    provider="opencode", model="opencode-go/deepseek-v4-pro"
)
CANDIDATE_OPENCODE_MUSE_SPARK = WorkerCandidate(
    provider="opencode", model="opencode-go/muse-spark-1.2-contributor"
)
CANDIDATE_OPENCODE_OX_ALPHA = WorkerCandidate(
    provider="opencode", model="opencode-go/ox-alpha-free"
)
CANDIDATE_CODEX_TERRA = WorkerCandidate(provider="codex", model="gpt-5.6-terra")
CANDIDATE_CODEX_SOL = WorkerCandidate(provider="codex", model="gpt-5.6-sol")
CANDIDATE_CODEX_GPT_5_6_LUNA = WorkerCandidate(provider="codex", model="gpt-5.6-luna")

# OpenCode Zen free worker candidates
CANDIDATE_OPENCODE_BIG_PICKLE = WorkerCandidate(provider="opencode", model="opencode/big-pickle")
CANDIDATE_OPENCODE_MIMO_V2_5_FREE = WorkerCandidate(
    provider="opencode", model="opencode/mimo-v2.5-free"
)
CANDIDATE_OPENCODE_HY3_FREE = WorkerCandidate(provider="opencode", model="opencode/hy3-free")
CANDIDATE_OPENCODE_NEMOTRON_3_ULTRA_FREE = WorkerCandidate(
    provider="opencode", model="opencode/nemotron-3-ultra-free"
)
CANDIDATE_OPENCODE_NEMOTRON_3_5_LIGHTNING_FREE = WorkerCandidate(
    provider="opencode", model="opencode/nemotron-3.5-lightning-free"
)
CANDIDATE_OPENCODE_MUSE_SPARK_FREE = WorkerCandidate(
    provider="opencode", model="opencode/muse-spark-1.2-contributor-free"
)

# OpenCode Zen paid worker candidates
CANDIDATE_OPENCODE_DEEPSEEK_V4_FLASH = WorkerCandidate(
    provider="opencode", model="opencode/deepseek-v4-flash"
)
CANDIDATE_OPENCODE_MINIMAX_M3 = WorkerCandidate(provider="opencode", model="opencode/minimax-m3")
CANDIDATE_OPENCODE_QWEN3_7_PLUS = WorkerCandidate(
    provider="opencode", model="opencode/qwen3.7-plus"
)
CANDIDATE_OPENCODE_DEEPSEEK_V4_PRO = WorkerCandidate(
    provider="opencode", model="opencode/deepseek-v4-pro"
)

# Tier candidate chains
TIER_1_CHAIN: list[WorkerCandidate] = [
    CANDIDATE_ANTIGRAVITY_FLASH,
    CANDIDATE_OPENCODE_MIMO_V2_5,
    CANDIDATE_OPENCODE_GO_DEEPSEEK_V4_FLASH,
]

TIER_2_TRAINING_ALLOWED_CHAIN: list[WorkerCandidate] = [
    CANDIDATE_OPENCODE_MUSE_SPARK,
    CANDIDATE_OPENCODE_GO_DEEPSEEK_V4_PRO,
    CANDIDATE_CODEX_TERRA,
    CANDIDATE_OPENCODE_GO_DEEPSEEK_V4_FLASH,
]

TIER_2_OFF_PEAK_CHAIN: list[WorkerCandidate] = [
    CANDIDATE_OPENCODE_GO_DEEPSEEK_V4_PRO,
    CANDIDATE_CODEX_TERRA,
    CANDIDATE_OPENCODE_GO_DEEPSEEK_V4_FLASH,
]

TIER_2_PEAK_CHAIN: list[WorkerCandidate] = [
    CANDIDATE_CODEX_TERRA,
    CANDIDATE_OPENCODE_GO_DEEPSEEK_V4_PRO,
    CANDIDATE_OPENCODE_GO_DEEPSEEK_V4_FLASH,
]

TIER_3_OFF_PEAK_CHAIN: list[WorkerCandidate] = [
    CANDIDATE_OPENCODE_GO_DEEPSEEK_V4_PRO,
    CANDIDATE_CODEX_TERRA,
    CANDIDATE_CODEX_SOL,
]

TIER_3_PEAK_CHAIN: list[WorkerCandidate] = [
    CANDIDATE_CODEX_TERRA,
    CANDIDATE_OPENCODE_GO_DEEPSEEK_V4_PRO,
    CANDIDATE_CODEX_SOL,
]


PEAK_HOURS_UTC_WINDOWS: tuple[tuple[int, int], ...] = (
    (1, 4),  # [01:00, 04:00) UTC
    (6, 10),  # [06:00, 10:00) UTC
)


def is_peak_hours(now: datetime | None = None) -> bool:
    """Determine if a datetime is within peak traffic hours in UTC.

    Peak windows as of 2026-08-22:
      - [01:00, 04:00) UTC
      - [06:00, 10:00) UTC
      - All other times: off-peak
      - No weekday/weekend distinction.
    """
    if now is None:
        dt = datetime.now(UTC)
    elif now.tzinfo is None:
        dt = now.replace(tzinfo=UTC)
    else:
        dt = now.astimezone(UTC)

    hour = dt.hour
    return any(start <= hour < end for start, end in PEAK_HOURS_UTC_WINDOWS)


def is_muse_model(model_name: str) -> bool:
    """Check if model name refers to a Muse model variant."""
    return "muse" in model_name.lower()


def parse_candidate_override(
    model_override: str,
    provider_override: str | None = None,
) -> WorkerCandidate:
    """Parse user-specified model and optional provider into a WorkerCandidate.

    Supports provider-qualified strings:
      - 'codex/gpt-5.6-terra'
      - 'antigravity/gemini-3.7-flash'
      - 'opencode/opencode-go/deepseek-v4-pro'
      - 'opencode-go/deepseek-v4-pro' (inferred as opencode)
      - 'custom_provider/custom_model'
    """
    model_clean = model_override.strip()
    if provider_override:
        return WorkerCandidate(provider=provider_override.strip().lower(), model=model_clean)

    if "/" in model_clean:
        prefix, remainder = model_clean.split("/", 1)
        prefix_lower = prefix.lower()
        if prefix_lower in ("antigravity", "agy", "google"):
            return WorkerCandidate(provider="antigravity", model=remainder.strip())
        elif prefix_lower == "opencode-go":
            return WorkerCandidate(provider="opencode", model=model_clean)
        elif prefix_lower == "cline-pass":
            # ClinePass models keep the full cline-pass/<model-id> identity as
            # the Orch model while routing to the "cline" Orch provider; the
            # adapter passes the full id to the Cline CLI's -m flag.
            return WorkerCandidate(provider="cline", model=model_clean)
        else:
            return WorkerCandidate(provider=prefix_lower, model=remainder.strip())

    # Inferred provider by model family
    lower = model_clean.lower()
    if "terra" in lower or "sol" in lower:
        return WorkerCandidate(provider="codex", model=model_clean)
    if "gemini" in lower:
        return WorkerCandidate(provider="antigravity", model=model_clean)

    return WorkerCandidate(provider="opencode", model=model_clean)


def resolve_candidate_chain(
    tier: Tier = Tier.T1,
    training_allowed: bool = True,
    is_peak: bool | None = None,
    model_override: str | None = None,
    provider_override: str | None = None,
    snapshot: Any | None = None,
) -> list[WorkerCandidate]:
    """Resolve ordered list of worker candidates to attempt based on tier, training, and peak state.

    Each :class:`WorkerCandidate` carries the exact C11-B ``binding_id`` the
    owner-approved :class:`~orchestrator_mvp.routing_config.DynamicCandidate`
    records, when present.  Static (closed-registry) candidates leave
    ``binding_id`` empty so they resolve through the existing C11-B
    binding-registry declaration order -- the historical backward-compatible
    behavior.

    Args:
        tier: Execution tier (T1, T2, T3).
        training_allowed: Whether training data collection is permitted.
        is_peak: Optional override for peak hours; defaults to is_peak_hours().
        model_override: Explicit model specified by user.
        provider_override: Explicit provider specified by user. When supplied without
            a model override, the resolved chain is filtered to that provider.

    Returns:
        List of WorkerCandidate instances to try in priority order.  Each
        candidate's ``binding_id`` is the durable, restart-deterministic
        C11-B execution identity the autonomous supervisor and dispatch
        lifecycle must consume; provider/model alone never silently
        substitutes for it (C15-XB-01).

    Raises:
        ValueError: If a Muse model is requested when training is denied, or if a
            provider override has no eligible candidates in the resolved chain.
    """
    if model_override:
        candidate = parse_candidate_override(model_override, provider_override=provider_override)
        cid = f"{candidate.provider}/{candidate.model}"
        if not training_allowed and (
            is_muse_model(candidate.model)
            or requires_training_permission(cid)
            or requires_training_permission(candidate.model)
        ):
            if is_muse_model(candidate.model):
                raise ValueError(
                    f"Prohibited model '{candidate.model}': Muse models require training "
                    "permission. Specify --training-allowed if permitted."
                )
            raise ValueError(
                f"Prohibited model '{candidate.model}': model requires training permission. "
                "Specify --training-allowed if permitted."
            )
        return [_with_binding_id(candidate, snapshot)]

    peak = is_peak if is_peak is not None else is_peak_hours()

    if snapshot is not None:
        raw_chain = snapshot.chain_for(tier.value, training_allowed, peak)
        chain = [_with_binding_id(c, snapshot) for c in raw_chain]
    elif tier == Tier.T1:
        chain = list(TIER_1_CHAIN)
    elif tier == Tier.T2:
        if training_allowed:
            chain = list(TIER_2_TRAINING_ALLOWED_CHAIN)
        elif peak:
            chain = list(TIER_2_PEAK_CHAIN)
        else:
            chain = list(TIER_2_OFF_PEAK_CHAIN)
    elif tier == Tier.T3:
        if peak:
            chain = list(TIER_3_PEAK_CHAIN)
        else:
            chain = list(TIER_3_OFF_PEAK_CHAIN)
    else:
        chain = list(TIER_1_CHAIN)

    if not training_allowed:
        chain = [
            c
            for c in chain
            if not is_muse_model(c.model)
            and not requires_training_permission(f"{c.provider}/{c.model}")
            and not requires_training_permission(c.model)
        ]

    if provider_override:
        provider_norm = provider_override.strip().lower()
        chain = [c for c in chain if c.provider == provider_norm]
        if not chain:
            raise ValueError(
                f"Provider override '{provider_override}' has no eligible candidates "
                "in the resolved chain for this tier/training/peak configuration."
            )

    return chain


def _with_binding_id(
    candidate: WorkerCandidate, snapshot: Any | None
) -> WorkerCandidate:
    """Return ``candidate`` enriched with its owner-approved binding_id.

    The lookup consults the snapshot's ``approved_candidates`` mapping:
    when a :class:`~orchestrator_mvp.routing_config.DynamicCandidate`
    entry exists for this ``provider/model`` identity, its exact
    ``binding_id`` is propagated onto the returned :class:`WorkerCandidate`
    so the autonomous supervisor and dispatch lifecycle execute through
    that binding (C15-XB-01).  Static candidates carry no approval record
    and return the original candidate unchanged -- the historical
    declaration-order behavior is preserved.
    """
    if snapshot is None:
        return candidate
    approved = getattr(snapshot, "approved_candidates", None)
    if not approved:
        return candidate
    cid = f"{candidate.provider}/{candidate.model}"
    for entry in approved:
        if getattr(entry, "candidate_id", None) != cid:
            continue
        binding_id = getattr(entry, "binding_id", None)
        if not binding_id:
            return candidate
        return WorkerCandidate(
            provider=candidate.provider,
            model=candidate.model,
            binding_id=str(binding_id),
        )
    return candidate
