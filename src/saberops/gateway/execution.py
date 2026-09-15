"""C10 — Typed execution disposition for gateway-aware worker dispatch.

This module owns the *single* gateway execution decision that the
workflow layer applies to a worker dispatch.  It exists separately
from :mod:`saberops.gateway.dispatch` because that module is
the local-selection seam (no HTTP) that has been in place since C10-R1;
this module is the C10-R3 *execution* boundary that actually wires
the gateway transport into a real Codex dispatch.

The contract:

* DIRECT: no gateway.  The worker is invoked with no augmentation.
* GATEWAY_READY: the gateway backend is configured and the worker is
  invoked through it.  Exactly one worker execution per Orch semantic
  attempt.
* SAFE_DIRECT_FALLBACK: the gateway *preparation* failed before any
  semantic request began.  The same provider/model is invoked through
  the ordinary DIRECT path with durable evidence.
* GATEWAY_BLOCKED: gateway enabled and direct fallback forbidden.
  No worker execution.  This is infrastructure blockage, not a model
  capability failure.
* AMBIGUOUS_FORWARDING: the gateway-configured worker process began a
  semantic request and a subsequent failure was observed.  No
  automatic replay.  Infrastructure uncertainty is durably persisted.

The disposition drives one Orch decision tree and is consumed at most
once per worker attempt.  It is the canonical C10-R3 seam that the
workflow layer consults to choose between code paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from saberops.gateway.bridge import BridgeEvidence, BridgeForwardingState


class ExecutionDisposition(StrEnum):
    """Typed execution disposition for one C10-aware worker dispatch."""

    DIRECT = "DIRECT"
    GATEWAY_READY = "GATEWAY_READY"
    SAFE_DIRECT_FALLBACK = "SAFE_DIRECT_FALLBACK"
    GATEWAY_BLOCKED = "GATEWAY_BLOCKED"
    AMBIGUOUS_FORWARDING = "AMBIGUOUS_FORWARDING"


@dataclass(frozen=True)
class GatewayExecutionContext:
    """Inputs the workflow layer hands to :func:`decide_gateway_execution`.

    All fields are non-secret.  The bridge session id is a UUID used to
    correlate persisted evidence.
    """

    frozen_digest: str | None
    endpoint: str | None
    requested_account_id: str | None
    selection_reason: str | None
    provider: str
    model: str
    bridge_loopback_url: str | None
    bridge_session_id: str | None
    direct_fallback_allowed: bool
    account_mismatch_detected: bool = False
    bridge_evidence: BridgeEvidence | None = None
    forwarding_state: BridgeForwardingState | None = None
    last_error: str | None = None
    # C10-R4: sticky vs latest forwarding aggregation
    any_request_forwarded: bool = False
    request_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "frozen_digest": self.frozen_digest,
            "endpoint": self.endpoint,
            "requested_account_id": self.requested_account_id,
            "selection_reason": self.selection_reason,
            "provider": self.provider,
            "model": self.model,
            "bridge_loopback_url": self.bridge_loopback_url,
            "bridge_session_id": self.bridge_session_id,
            "direct_fallback_allowed": self.direct_fallback_allowed,
            "account_mismatch_detected": self.account_mismatch_detected,
            "forwarding_state": (
                self.forwarding_state.value if self.forwarding_state is not None else None
            ),
            "last_error": self.last_error,
            "any_request_forwarded": self.any_request_forwarded,
            "request_count": self.request_count,
            "bridge_evidence": (
                self.bridge_evidence.as_dict() if self.bridge_evidence is not None else None
            ),
        }


@dataclass(frozen=True)
class GatewayExecutionDecision:
    """Result of the gateway execution boundary.

    ``disposition`` is the canonical C10-R3 routing signal.  ``reason``
    is a short, non-secret description suitable for durable evidence;
    ``evidence`` is the optional bridge row that supports the decision.
    """

    disposition: ExecutionDisposition
    reason: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "reason": self.reason,
            "evidence": dict(sorted(self.evidence.items())),
        }


def decide_gateway_execution(
    context: GatewayExecutionContext,
) -> GatewayExecutionDecision:
    """Resolve the typed execution disposition for one C10-aware dispatch.

    The rule order is fixed:

    1. Positive observed account mismatch (sticky)
       → ``GATEWAY_BLOCKED`` (the gateway returned a different account).
    2. Bridge forwarding state already past ``NOT_STARTED`` but no
       successful completion OR any_request_forwarded with later ambiguous
       failure → ``AMBIGUOUS_FORWARDING`` (a semantic request began).
    3. Bridge forwarding state ``REQUEST_COMPLETED`` without sticky mismatch
       → ``GATEWAY_READY`` (the gateway executed the request).
    4. Bridge never received a request
       → ``SAFE_DIRECT_FALLBACK`` (pre-launch, allowed) or
         ``GATEWAY_BLOCKED`` (forbidden).
    5. Bridge is configured and ready (loopback URL is set)
       → ``GATEWAY_READY`` (the worker will execute through it).
       Pre-launch missing bridge also follows fallback policy.
    """

    has_bridge = bool(context.bridge_loopback_url and context.bridge_session_id)
    forwarding_state = context.forwarding_state
    # Derive sticky/any forwarded from bridge evidence if not explicitly set
    any_forwarded = context.any_request_forwarded
    if context.bridge_evidence is not None:
        any_forwarded = any_forwarded or bool(context.bridge_evidence.any_request_forwarded)
        # Also consider request_count >0 as forwarded
        if context.bridge_evidence.request_count > 0:
            any_forwarded = any_forwarded or context.bridge_evidence.any_request_forwarded

    if context.account_mismatch_detected:
        return GatewayExecutionDecision(
            disposition=ExecutionDisposition.GATEWAY_BLOCKED,
            reason="gateway_account_mismatch",
            evidence=context.as_dict(),
        )

    # If any request was forwarded and the final state is not clean completion,
    # or if we have evidence of sticky mismatch already handled above,
    # ambiguous forwarding applies.
    if forwarding_state in {
        BridgeForwardingState.REQUEST_FORWARDED,
        BridgeForwardingState.REQUEST_FAILED,
    }:
        return GatewayExecutionDecision(
            disposition=ExecutionDisposition.AMBIGUOUS_FORWARDING,
            reason="forwarding_unknown_post_launch",
            evidence=context.as_dict(),
        )

    # Sticky ambiguous: any request forwarded but now in REQUEST_COMPLETED
    # yet later worker failed ambiguously? The boundary's finalize will set
    # forwarding_state to REQUEST_COMPLETED for successful last request,
    # but if worker later fails ambiguous, we need to propagate that via
    # bridge_evidence any_request_forwarded. However decide_gateway_execution
    # alone cannot know worker failure; that is handled by boundary.finalize
    # checking worker result. Here we only handle pure forwarding states.
    # For multi-request sticky forwarded but later completed, we still want
    # GATEWAY_READY unless worker failed — that check lives in boundary.

    if forwarding_state is BridgeForwardingState.REQUEST_COMPLETED:
        return GatewayExecutionDecision(
            disposition=ExecutionDisposition.GATEWAY_READY,
            reason="gateway_request_completed",
            evidence=context.as_dict(),
        )

    # If no bridge configured at all (pre-launch)
    if not has_bridge:
        if context.direct_fallback_allowed:
            return GatewayExecutionDecision(
                disposition=ExecutionDisposition.SAFE_DIRECT_FALLBACK,
                reason="pre_launch_fallback_allowed",
                evidence=context.as_dict(),
            )
        return GatewayExecutionDecision(
            disposition=ExecutionDisposition.GATEWAY_BLOCKED,
            reason="pre_launch_fallback_forbidden",
            evidence=context.as_dict(),
        )

    # Bridge configured and ready pre-launch, no request yet
    if forwarding_state in (BridgeForwardingState.NOT_STARTED, BridgeForwardingState.UNKNOWN, None):
        # This is pre-launch ready state — worker will execute through bridge
        # If direct fallback not relevant, we are GATEWAY_READY
        # But if bridge never receives request (request_count 0) after worker,
        # post-launch logic will handle via finalize's UNKNOWN handling.
        # For pre-launch, report GATEWAY_READY.
        # However if endpoint missing or similar already handled before bridge creation,
        # we wouldn't be here.
        return GatewayExecutionDecision(
            disposition=ExecutionDisposition.GATEWAY_READY,
            reason="gateway_ready_pre_launch",
            evidence=context.as_dict(),
        )

    # Fallback: treat as ambiguous if any forwarding occurred
    if any_forwarded:
        return GatewayExecutionDecision(
            disposition=ExecutionDisposition.AMBIGUOUS_FORWARDING,
            reason="forwarding_unknown_post_launch",
            evidence=context.as_dict(),
        )

    return GatewayExecutionDecision(
        disposition=ExecutionDisposition.GATEWAY_READY,
        reason="gateway_ready_pre_launch",
        evidence=context.as_dict(),
    )


__all__ = [
    "ExecutionDisposition",
    "GatewayExecutionContext",
    "GatewayExecutionDecision",
    "decide_gateway_execution",
]
