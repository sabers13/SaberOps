"""Typed project-instruction transport compatibility evidence for OpenCode.

Reproduced constraint (OpenCode 1.18.x, General Compute upstream):

* ``generalcompute/minimax-m2.7`` and ``generalcompute/deepseek-v3.1``
  succeed for harmless prose project instructions but fail with an opaque
  upstream ``403 Forbidden`` as soon as the request carries command-like
  project instruction text (for example the canonical
  ``python -m orchestrator_mvp.devscope scope`` scope-first instruction,
  with or without backticks).
* ``OPENCODE_DISABLE_PROJECT_CONFIG=1`` avoids the automatic project
  injection, but the same literal command string inside the task itself
  still fails with ``403`` -- so no meaning-preserving transport
  adaptation exists that keeps the exact executable command the worker
  itself must type.  A prose paraphrase passes the transport but strips
  the unambiguous operation identity, which is NOT equivalent governance.

Compatibility is EVIDENCE, never routing.  This module reports factual,
deterministic transport evidence only.  It never decides routing (never
"use another model", never "change the routing chain"): Orch policy / C07
preflight consumes the evidence and owns eligibility.  Compatibility
belongs to the ``provider + backend + transport`` combination (OpenCode
CLI calling a specific upstream), never to a bare model name.

Contract scope (OPTION B -- narrow, truthful):

* ``FULL`` -- positive evidence exists that this upstream/transport
  combination faithfully carries the canonical project instruction
  contract.  It is only returned for an explicit, provider-owned set of
  upstream namespaces that have been positively verified; it is never
  inferred from "not General Compute", from a parseable model string, or
  from the mere fact that OpenCode can list/address the model.
* ``ADAPTED`` -- reserved for a future *verified* meaning-preserving
  adaptation.  Nothing in this repository currently returns it; it must
  never be claimed without proof.
* ``INCOMPATIBLE`` -- proven (by the reproduction above) to be unable to
  carry command-like governed instructions for this transport.
* ``UNKNOWN`` -- compatibility has not been established: empty /
  unparseable identities, and every upstream that is not in the verified
  compatible set.  Must fail closed wherever guaranteed governance
  delivery is required.

Adapters that do not implement the ``instruction_compatibility`` hook
are not implicitly certified ``FULL``.  The contract is silent about
them: ``requires_governed_instructions=True`` simply does not apply to
adapters that have no opinion about project-instruction transport, the
same way it does not apply to transports that have not been reproduced.
This is the smaller architecture that matches the actual existing
semantics: we have only reproduced the OpenCode + General Compute
constraint, so only OpenCode currently opts into this contract.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "GENERAL_COMPUTE_MODEL_PREFIX",
    "GOVERNED_INSTRUCTIONS_USER_MESSAGE",
    "VERIFIED_COMPATIBLE_OPENCODE_UPSTREAM_PREFIXES",
    "InstructionCompatibility",
    "instruction_compatibility_for_opencode_model",
    "is_general_compute_model",
    "is_verified_compatible_opencode_upstream",
]


class InstructionCompatibility(StrEnum):
    """Factual instruction-carrying status for one transport/model pair."""

    FULL = "FULL"
    ADAPTED = "ADAPTED"
    INCOMPATIBLE = "INCOMPATIBLE"
    UNKNOWN = "UNKNOWN"


#: Canonical OpenCode model-id prefix for the General Compute upstream
#: (``generalcompute/<model>``).  Matched case-insensitively after
#: stripping surrounding whitespace.
GENERAL_COMPUTE_MODEL_PREFIX = "generalcompute/"

#: Explicit provider-owned evidence map: OpenCode upstream namespaces
#: POSITIVELY VERIFIED to carry the canonical project instruction
#: contract faithfully (the OpenCode-native "Zen" upstreams
#: ``opencode`` and ``opencode-go``).
#:
#: Evidence (existing real successful OpenCode operation in this governed
#: repository, whose worktrees carry the canonical ``AGENTS.md``):
#: hundreds of governed worker dispatches through both upstreams --
#: ``opencode`` (``deepseek-v4-pro``/``deepseek-v4-flash``/
#: ``minimax-m3``/``muse-spark-*``) and ``opencode-go``
#: (``deepseek-v4-pro``/``deepseek-v4-flash``/``mimo-v2.5``/
#: ``muse-spark-1.2-contributor``/``ox-alpha-free``/``qwen3.8-max``/
#: ``kimi-k3``) -- completed in ``.orch-worktrees/orchestrator-v2/run_*``
#: worktrees of this repository with ``AGENTS.md`` present.  The
#: reproduced 403 constraint is specific to the General Compute upstream.
#:
#: This is deliberately a small, deterministic prefix map and not a
#: generic policy DSL or a second routing registry: it carries factual
#: evidence only, and C07 still owns eligibility.  Adding a namespace
#: here without concrete positive evidence is forbidden; unlisted
#: namespaces stay ``UNKNOWN`` and fail closed.
VERIFIED_COMPATIBLE_OPENCODE_UPSTREAM_PREFIXES = frozenset(
    {
        "opencode/",
        "opencode-go/",
    }
)

#: Non-secret user-facing reason for a governed-instruction refusal.
#: Carries no credentials, tokens, or request material.
GOVERNED_INSTRUCTIONS_USER_MESSAGE = (
    "Provider transport cannot faithfully carry this project's required instruction contract."
)


def is_general_compute_model(model: str | None) -> bool:
    """Return True iff ``model`` addresses the General Compute upstream.

    Pure, deterministic, and total: ``None`` / empty / whitespace-only
    input returns ``False`` (absence of evidence, never a positive
    claim).  Matching is case-insensitive over the canonical
    ``generalcompute/`` provider prefix; a bare ``generalcompute``
    identity (no model suffix) is treated as General Compute as well so
    a truncated identity can never slip through as non-GC.
    """
    if model is None:
        return False
    normalized = model.strip().lower()
    if not normalized:
        return False
    return normalized == "generalcompute" or normalized.startswith(GENERAL_COMPUTE_MODEL_PREFIX)


def is_verified_compatible_opencode_upstream(model: str | None) -> bool:
    """Return True iff ``model`` belongs to the explicit verified-compatible set.

    Pure, deterministic, and total.  Only upstream namespaces with
    positive factual evidence of faithful project-instruction carriage
    are listed; a bare ``opencode``/``opencode-go`` identity with no
    model suffix is malformed evidence and returns ``False`` (it is
    ``UNKNOWN``, not ``FULL``).  Matching is case-insensitive after
    stripping surrounding whitespace.
    """
    if model is None:
        return False
    normalized = model.strip().lower()
    if not normalized:
        return False
    for prefix in VERIFIED_COMPATIBLE_OPENCODE_UPSTREAM_PREFIXES:
        remainder = normalized[len(prefix) :]
        if normalized.startswith(prefix) and remainder and not remainder.startswith("/"):
            return True
    return False


def instruction_compatibility_for_opencode_model(
    model: str | None,
) -> InstructionCompatibility:
    """Report instruction compatibility for one OpenCode ``model`` id.

    * General Compute models (``generalcompute/...``) -> ``INCOMPATIBLE``:
      proven by reproduction to 403 on command-like governed
      instructions, including the canonical scope-first command the
      worker itself must type.
    * Explicitly verified compatible upstreams
      (``VERIFIED_COMPATIBLE_OPENCODE_UPSTREAM_PREFIXES``) -> ``FULL``:
      these namespaces have positive evidence of faithfully carrying the
      canonical project instruction contract.
    * Empty / unparseable identities -> ``UNKNOWN`` (fail closed where
      governed delivery must be proven).
    * Every other (unrecognized) upstream -> ``UNKNOWN``.  A previously
      unseen upstream is never certified ``FULL``; discovery
      availability and instruction compatibility are different facts.

    ``ADAPTED`` is never returned: no meaning-preserving adaptation has
    been proven for any OpenCode transport combination.
    """
    if model is None or not model.strip():
        return InstructionCompatibility.UNKNOWN
    if is_general_compute_model(model):
        return InstructionCompatibility.INCOMPATIBLE
    if is_verified_compatible_opencode_upstream(model):
        return InstructionCompatibility.FULL
    return InstructionCompatibility.UNKNOWN
