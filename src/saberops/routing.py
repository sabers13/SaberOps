"""Tier-only routing and multi-provider candidate resolution."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from saberops.models import Tier, WorkerCandidate

# Canonical worker candidates
#
# Antigravity is attended-only (ADR-0001 Amendment 1, item 3): the
# adapter class below is kept for explicit owner overrides, but the
# automatic candidate below must never appear in an automatic chain.
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

# Tier candidate chains were removed in R-002 (routing schema 3): chains
# now live per role in the routing configuration. The candidate constants
# above stay: review.py imports several of them.

#: Providers restricted to attended, explicit-owner use (ADR-0001
#: Amendment 1).  Automatic routing chains must never yield them, even
#: when a frozen snapshot or owner document still lists them.
ATTENDED_ONLY_PROVIDERS: frozenset[str] = frozenset({"antigravity"})

#: Refusal for any non-explicit selection of an attended-only provider.
#: It names the governing reason (ADR-0001 Amendment 1) and the one
#: supported path (an explicit owner provider+model override).
ATTENDED_ONLY_REFUSAL = (
    "Antigravity is attended-only per ADR-0001 (Amendment 1, item 3): "
    "Google's terms prohibit third-party automation of Antigravity, so "
    "automatic routing cannot select provider 'antigravity'. Re-run with "
    "an explicit owner provider+model override to run it attended."
)


def is_peak_hours(now: datetime | None = None) -> bool:
    """Retired compatibility helper; routing has no peak-hour mode."""
    return False  # Retired compatibility helper: no scheduling exclusions.


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


def _normalize_role(role: Any) -> str:
    from saberops.jobs.models import JobRole

    if isinstance(role, JobRole):
        return role.value
    return str(role).strip().upper()


def _role_for_tier_alias(tier: Any | None) -> str:
    if tier is None:
        return "WORKER"
    if isinstance(tier, Tier):
        return "SURGEON" if tier == Tier.T3 else "WORKER"
    try:
        normalized = Tier(str(tier).strip().upper())
    except ValueError:
        return "WORKER"
    return "SURGEON" if normalized == Tier.T3 else "WORKER"


def resolve_candidate_chain(
    role: Any | None = None,
    *,
    tier: Any | None = None,
    training_allowed: bool = True,
    is_peak: bool | None = None,
    model_override: str | None = None,
    provider_override: str | None = None,
    snapshot: Any | None = None,
) -> list[WorkerCandidate]:
    """Resolve ordered list of worker candidates for one role chain.

    Attended-only providers (``ATTENDED_ONLY_PROVIDERS``) are excluded
    from every automatic chain, even when configured; only an explicit
    owner ``model_override`` (with or without ``provider_override``) may
    yield them.  A provider-only override naming an attended-only
    provider is refused with :data:`ATTENDED_ONLY_REFUSAL`.

    Each :class:`WorkerCandidate` carries the exact ``binding_id`` the
    owner-approved :class:`~saberops.routing_config.DynamicCandidate`
    records, when present.  Static (closed-registry) candidates leave
    ``binding_id`` empty so they resolve through the existing
    binding-registry declaration order -- the historical backward-compatible
    behavior.

    Args:
        role: Routing role (WORKER, SURGEON, INTEGRATOR).
        tier: Deprecated alias (ADR-0002 §11): ``None`` selects WORKER,
            ``T3`` selects SURGEON, anything else selects WORKER.
            Passing both ``role`` and ``tier`` raises ``ValueError``.
        training_allowed: Ignored legacy compatibility argument.
        is_peak: Ignored legacy compatibility argument.
        model_override: Explicit model specified by user.
        provider_override: Explicit provider specified by user. When supplied without
            a model override, the resolved chain is filtered to that provider.

    Returns:
        List of WorkerCandidate instances to try in priority order.  Each
        candidate's ``binding_id`` is the durable, restart-deterministic
        Execution identity the autonomous supervisor and dispatch
        lifecycle must consume; provider/model alone never silently
        substitutes for it.

    Raises:
        ValueError: If both ``role`` and ``tier`` are given, or if a
            provider override has no candidates in the selected chain,
            or if a provider-only override names an attended-only provider
            (Antigravity requires an explicit owner provider+model override).
    """
    if role is not None and tier is not None:
        raise ValueError("resolve_candidate_chain: pass 'role' or 'tier', not both")
    selected_role = _normalize_role(role) if role is not None else _role_for_tier_alias(tier)
    if model_override:
        candidate = parse_candidate_override(model_override, provider_override=provider_override)
        return [_with_frozen_evidence(candidate, snapshot)]

    if provider_override is not None:
        if provider_override.strip().lower() in ATTENDED_ONLY_PROVIDERS:
            raise ValueError(ATTENDED_ONLY_REFUSAL)

    if snapshot is None:
        from saberops.routing_config import load_packaged_default

        snapshot = load_packaged_default()
    chain = [
        _with_frozen_evidence(c, snapshot) for c in snapshot.chain_for_role(selected_role)
    ]

    # Attended-only providers never dispatch automatically, even when a
    # frozen snapshot or owner document still lists them.  An empty chain
    # is a valid owner decision: dispatch terminates with structured
    # no-eligible-candidate evidence, never a substituted default.
    chain = [c for c in chain if c.provider not in ATTENDED_ONLY_PROVIDERS]

    if provider_override:
        provider_norm = provider_override.strip().lower()
        chain = [c for c in chain if c.provider == provider_norm]
        if not chain:
            raise ValueError(
                f"Provider override '{provider_override}' has no eligible candidates "
                "in the resolved chain for this role."
            )

    return chain


def _with_frozen_evidence(candidate: WorkerCandidate, snapshot: Any | None) -> WorkerCandidate:
    """Return ``candidate`` enriched with its frozen owner-approved evidence.

    The lookup consults only the frozen snapshot's ``approved_candidates``
    mapping (never mutable live state): when a
    :class:`~saberops.routing_config.DynamicCandidate` entry exists for
    this ``provider/model`` identity, its exact ``binding_id`` is
    propagated onto the returned :class:`WorkerCandidate` so the
    autonomous supervisor and dispatch lifecycle execute through that
    binding, and its frozen ``training_required``
    classification is carried so the canonical data-policy decision
    sees the same evidence at every dispatch path.  Static candidates
    carry no approval record and return the original candidate unchanged
    -- the historical declaration-order behavior is preserved.
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
        training_required = getattr(entry, "training_required", None)
        if isinstance(training_required, str):
            training_required = training_required.strip().upper() or None
        else:
            training_required = None
        if training_required not in ("TRUE", "FALSE", "UNKNOWN"):
            training_required = None
        if not binding_id and training_required is None:
            return candidate
        return WorkerCandidate(
            provider=candidate.provider,
            model=candidate.model,
            binding_id=str(binding_id) if binding_id else candidate.binding_id,
            training_required=training_required,
        )
    return candidate


def _with_binding_id(candidate: WorkerCandidate, snapshot: Any | None) -> WorkerCandidate:
    """Backward-compatible alias for :func:`_with_frozen_evidence`."""
    return _with_frozen_evidence(candidate, snapshot)
