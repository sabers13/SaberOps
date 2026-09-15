"""C10 — Workflow-layer gateway execution boundary.

This module owns the *one* shared gateway execution boundary that the
orchestrator applies to every C10-enabled worker dispatch (initial,
retry, repair, and review).  It encapsulates:

* ``prepare_gateway_execution`` — selects an account/connection, starts
  the bounded bridge, returns a typed :class:`GatewayExecutionDecision`
  together with the augmented :class:`WorkerRequest`.
* ``finalize_gateway_execution`` — stops the bridge, captures the
  bounded evidence row, and returns the bridge :class:`BridgeEvidence`
  together with the post-launch :class:`ExecutionDisposition`.

The boundary is intentionally small.  All gateway knowledge lives in
:mod:`saberops.gateway`; this module glues the workflow
boundary to it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from saberops.gateway.accounts import AccountSelectionResult
from saberops.gateway.affinity import AffinityHint
from saberops.gateway.bridge import (
    BridgeErrorKind,
    BridgeEvidence,
    BridgeForwardingState,
    BridgeSession,
    build_loopback_bridge_session,
    serialize_bridge_evidence,
)
from saberops.gateway.contracts import (
    FrozenGatewayConfig,
    GatewayAccount,
    GatewayDispatchResult,
    GatewayDispatchTarget,
    GatewayError,
    GatewayErrorKind,
    GatewayKind,
    GatewayManifestError,
    GatewayQuotaObservation,
    GatewaySelection,
)
from saberops.gateway.dispatch import (
    GATEWAY_AUTHORIZATION_ENV,
    GATEWAY_AUTHORIZATION_ENV_DEFAULT,
    GATEWAY_CONNECTION_ENV,
    GATEWAY_ENDPOINT_ENV,
    GATEWAY_FROZEN_DIGEST_ENV,
    GATEWAY_LOOPBACK_ENV,
    GATEWAY_PROVIDER_ENV,
    GATEWAY_SELECTION_REASON_ENV,
    GatewayDispatcher,
    LocalSelectionTransport,
)
from saberops.gateway.dispatch import (
    prepare_dispatch_decision as _legacy_prepare_dispatch_decision,
)
from saberops.gateway.execution import (
    ExecutionDisposition,
    GatewayExecutionContext,
    decide_gateway_execution,
)
from saberops.gateway.health import GatewayHealthState, utc_now_iso
from saberops.gateway.quota import GatewayQuotaPoolView
from saberops.models import AttemptStatus, WorkerRequest, WorkerResult

# Sentinel env var name and fixed non-secret value supplied to Codex child
# when the loopback bridge is the transport owner. The bridge strips this
# sentinel Authorization and injects the real OmniRoute token at forwarding time.
_BRIDGE_SENTINEL_ENV = "_ORCH_BRIDGE_AUTH_SENTINEL"
_BRIDGE_SENTINEL_VALUE = "loopback-sentinel-not-a-secret"


def _public_gateway_env_names() -> tuple[str, ...]:
    """Return the bounded set of gateway env vars the workflow layer writes."""

    return (
        GATEWAY_ENDPOINT_ENV,
        GATEWAY_CONNECTION_ENV,
        GATEWAY_AUTHORIZATION_ENV,
        GATEWAY_FROZEN_DIGEST_ENV,
        GATEWAY_SELECTION_REASON_ENV,
        GATEWAY_PROVIDER_ENV,
        GATEWAY_LOOPBACK_ENV,
        _BRIDGE_SENTINEL_ENV,
    )


_DEFAULT_AUTHORIZATION_ENV_VAR = "OMNIROUTE_API_KEY"


def _authorization_env_var_for(
    frozen_gateway: FrozenGatewayConfig | None,
) -> str:
    """Return the configured non-secret authorization env-var name.

    Falls back to the public default (``OMNIROUTE_API_KEY``) when no
    frozen contract is supplied or the contract uses the default name.
    The VALUE never appears here — only the env-var NAME.
    """

    if frozen_gateway is None:
        return _DEFAULT_AUTHORIZATION_ENV_VAR
    name = getattr(frozen_gateway, "authorization_env_var", None)
    if not name or not isinstance(name, str):
        return _DEFAULT_AUTHORIZATION_ENV_VAR
    return name


def _sanitize_base_env(
    env: dict[str, str],
    *,
    frozen_gateway: FrozenGatewayConfig | None,
) -> dict[str, str]:
    """Boundary-owned sanitization applied to every launched worker env.

    Removes:

    * the exact configured ``FrozenGatewayConfig.authorization_env_var``;
    * the default ``OMNIROUTE_API_KEY`` when applicable;
    * ``_ORCH_BRIDGE_AUTH_SENTINEL``;
    * every reserved C10 gateway-control variable.

    The sanitization is single-source-of-truth: every launch path inside
    :func:`run_worker_with_gateway_boundary` (and the boundary's
    :meth:`GatewayExecutionBoundary.prepare`) routes through this helper,
    so the configured secret value can never survive a public/supported
    boundary invocation into :func:`run_adapter` regardless of which
    caller supplied the env. The orchestrator's own pre-scrub remains a
    defense-in-depth; the boundary no longer depends solely on it.

    The secret value is never persisted or logged by this helper.
    """

    cleaned = dict(env)
    configured = _authorization_env_var_for(frozen_gateway)
    cleaned.pop(configured, None)
    if configured != _DEFAULT_AUTHORIZATION_ENV_VAR:
        cleaned.pop(_DEFAULT_AUTHORIZATION_ENV_VAR, None)
    for key in _public_gateway_env_names():
        cleaned.pop(key, None)
    return cleaned


def _inherit_env_exclusions(
    base_request: WorkerRequest,
    *,
    frozen_gateway: FrozenGatewayConfig | None,
) -> tuple[str, ...]:
    """Merge caller exclusions with C10 names forbidden from parent inheritance."""

    c10_names = (
        _authorization_env_var_for(frozen_gateway),
        _DEFAULT_AUTHORIZATION_ENV_VAR,
        *_public_gateway_env_names(),
    )
    return tuple(dict.fromkeys((*base_request.inherit_env_exclude, *c10_names)))


def _sanitized_worker_request(
    base_request: WorkerRequest,
    *,
    frozen_gateway: FrozenGatewayConfig | None,
    env: dict[str, str],
) -> WorkerRequest:
    """Apply C10 explicit-env and inherited-parent isolation in one place."""

    return replace(
        base_request,
        env=env,
        inherit_env_exclude=_inherit_env_exclusions(
            base_request, frozen_gateway=frozen_gateway
        ),
    )


def _strip_gateway_env(env: dict[str, str]) -> dict[str, str]:
    """Backwards-compatible wrapper stripping reserved gateway vars only.

    Prefer :func(_sanitize_base_env) for new callers: it also scrubs the
    configured authorization reference and the default secret key. This
    wrapper is retained for callers that genuinely have no
    :class:`FrozenGatewayConfig` context and need only the gateway-var
    strip (e.g. legacy DIRECT-only paths).
    """

    for key in _public_gateway_env_names():
        env.pop(key, None)
    return env


def _gateway_error_from_bridge(
    bridge_evidence: BridgeEvidence,
    execution_status: str,
) -> GatewayError | None:
    """Map bounded bridge evidence into the typed GatewayError taxonomy."""

    status = bridge_evidence.status_code
    any_forwarded = bool(bridge_evidence.any_request_forwarded)
    forwarding = bridge_evidence.forwarding_state

    if status is not None:
        if status in (401, 403):
            return GatewayError(
                kind=GatewayErrorKind.AUTH_FAILED,
                message=f"gateway auth failure {status}",
                recoverable=False,
                request_forwarded=True,
            )
        if status == 429:
            return GatewayError(
                kind=GatewayErrorKind.QUOTA_UNAVAILABLE,
                message="gateway quota unavailable (429)",
                recoverable=False,
                request_forwarded=True,
            )
        if status in (404, 410):
            return GatewayError(
                kind=GatewayErrorKind.ACCOUNT_UNAVAILABLE,
                message=f"gateway account unavailable {status}",
                recoverable=False,
                request_forwarded=True,
            )
        if 500 <= status < 600:
            return GatewayError(
                kind=GatewayErrorKind.UNAVAILABLE,
                message=f"gateway server error {status}",
                recoverable=False,
                request_forwarded=True,
            )
        if 400 <= status < 500:
            return GatewayError(
                kind=GatewayErrorKind.PROTOCOL_ERROR,
                message=f"gateway protocol error {status}",
                recoverable=False,
                request_forwarded=True,
            )
    if bridge_evidence.error_kind is not None or bridge_evidence.last_error_kind is not None:
        kind = bridge_evidence.error_kind or bridge_evidence.last_error_kind
        err_msg = (
            bridge_evidence.error_message
            or bridge_evidence.last_error_message
            or "bridge failure"
        )
        lower = err_msg.lower()
        if "authorization not configured" in lower or "missing authorization" in lower:
            return GatewayError(
                kind=GatewayErrorKind.AUTH_FAILED,
                message=err_msg,
                recoverable=False,
                request_forwarded=False,
            )
        if "timeout" in lower:
            return GatewayError(
                kind=GatewayErrorKind.TIMEOUT,
                message=err_msg,
                recoverable=False,
                request_forwarded=any_forwarded,
            )
        if any(tok in lower for tok in ("protocol", "malformed", "unexpected http")):
            return GatewayError(
                kind=GatewayErrorKind.PROTOCOL_ERROR,
                message=err_msg,
                recoverable=False,
                request_forwarded=any_forwarded,
            )
        if kind in (BridgeErrorKind.FORWARD_FAILED, BridgeErrorKind.GATEWAY_ERROR):
            if "connect" in lower or "read" in lower or "forward failed" in lower:
                return GatewayError(
                    kind=GatewayErrorKind.UNAVAILABLE,
                    message=err_msg,
                    recoverable=False,
                    request_forwarded=any_forwarded,
                )
        if any_forwarded or forwarding == BridgeForwardingState.REQUEST_FAILED:
            return GatewayError(
                kind=GatewayErrorKind.UNAVAILABLE,
                message=err_msg,
                recoverable=False,
                request_forwarded=any_forwarded,
            )
        return GatewayError(
            kind=GatewayErrorKind.UNAVAILABLE,
            message=err_msg,
            recoverable=False,
            request_forwarded=False,
        )
    if (
        forwarding == BridgeForwardingState.REQUEST_FAILED
        and bridge_evidence.failed_request_count > 0
    ):
        return GatewayError(
            kind=GatewayErrorKind.UNAVAILABLE,
            message=bridge_evidence.error_message or "bridge forwarding failed",
            recoverable=False,
            request_forwarded=any_forwarded,
        )
    if execution_status == ExecutionDisposition.AMBIGUOUS_FORWARDING.value:
        if any_forwarded:
            return GatewayError(
                kind=GatewayErrorKind.UNAVAILABLE,
                message=bridge_evidence.error_message or "ambiguous forwarding after request",
                recoverable=False,
                request_forwarded=True,
            )
    return None


# ---------------------------------------------------------------------------
# Live state wiring — gateway-owned injectable runtime observation interface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GatewayLiveState:
    """Live, non-frozen gateway observations read per dispatch.

    Frozen config (kind, endpoint, permitted models, fallback policy) is
    the run's gateway *identity*. Live state (health, quota, pool,
    affinity) is infrastructure *eligibility* read fresh each dispatch
    and never frozen into the run contract. UNKNOWN observations remain
    UNKNOWN — never fabricated.
    """

    health_state: GatewayHealthState | None = None
    quota_observations: tuple[GatewayQuotaObservation, ...] = ()
    pool_observations: tuple[GatewayQuotaPoolView, ...] = ()
    affinity_hint: AffinityHint | None = None


class GatewayLiveStateProvider(Protocol):
    """Narrow, gateway-owned provider of live observations."""

    def get_live_state(
        self,
        *,
        provider: str,
        model: str,
        run_id: str | None = None,
    ) -> GatewayLiveState: ...


@dataclass
class GatewayExecutionBoundary:
    """Stateful C10-R3 execution boundary used by the orchestrator.

    The orchestrator constructs one boundary per dispatch attempt; the
    boundary owns the bridge session, the GatewayDispatcher instance,
    and the worker request after preparation.  Lifecycle:

    1. :meth:`prepare` selects an account, starts the bridge, and
       returns the augmented worker request and the typed decision.
    2. The orchestrator invokes the worker adapter with the augmented
       request.
    3. :meth:`finalize` stops the bridge, persists its evidence row,
       and yields the post-launch disposition.

    The boundary never makes a second semantic model request on its
    own.
    """

    frozen_gateway: FrozenGatewayConfig
    endpoint: str
    dispatcher: GatewayDispatcher
    accounts: tuple[GatewayAccount, ...] = field(default_factory=tuple)
    health_state: GatewayHealthState | None = None
    affinity_hint: AffinityHint | None = None
    quota_observations: tuple[GatewayQuotaObservation, ...] = field(default_factory=tuple)
    pool_observations: tuple[GatewayQuotaPoolView, ...] = field(default_factory=tuple)
    bridge: BridgeSession | None = field(default=None, init=False)
    augmented_request: WorkerRequest | None = field(default=None, init=False)
    selection_result: AccountSelectionResult | None = field(default=None, init=False)
    bridge_evidence: BridgeEvidence | None = field(default=None, init=False)
    pre_launch_decision: GatewayExecutionContext | None = field(default=None, init=False)
    # Original semantic candidate — never derived from selection_reason
    _requested_provider: str | None = field(default=None, init=False)
    _requested_model: str | None = field(default=None, init=False)

    @classmethod
    def from_frozen_config(
        cls,
        *,
        frozen_gateway: FrozenGatewayConfig,
        endpoint: str,
        accounts: tuple[GatewayAccount, ...] = (),
        health_state: GatewayHealthState | None = None,
        affinity_hint: AffinityHint | None = None,
        quota_observations: Iterable[GatewayQuotaObservation] = (),
        pool_observations: Iterable[GatewayQuotaPoolView] = (),
    ) -> GatewayExecutionBoundary:
        """Construct the boundary with the local-selection dispatcher.

        The dispatcher is used only to pick the equivalent account; it
        never makes an HTTP call to the gateway backend.  The bridge is
        the transport seam that actually carries the request.
        """

        if not endpoint:
            raise GatewayManifestError("gateway endpoint is required for boundary")
        dispatcher = GatewayDispatcher.from_frozen_config(
            frozen_config=frozen_gateway,
            accounts=accounts,
            health=health_state,
            omniroute_transport=LocalSelectionTransport(),
            omniroute_endpoint=endpoint,
        )
        if dispatcher.omniroute is None:
            raise GatewayManifestError(
                "OmniRoute gateway dispatcher could not be constructed for boundary"
            )
        return cls(
            frozen_gateway=frozen_gateway,
            endpoint=endpoint,
            dispatcher=dispatcher,
            accounts=tuple(accounts),
            health_state=health_state or GatewayHealthState.empty(),
            affinity_hint=affinity_hint,
            quota_observations=tuple(quota_observations),
            pool_observations=tuple(pool_observations),
        )

    # --- selection ----------------------------------------------------------

    def select_account(
        self,
        *,
        provider: str,
        model: str,
    ) -> AccountSelectionResult:
        if self.dispatcher.omniroute is None:
            raise GatewayManifestError("OmniRoute dispatcher not present on boundary")
        return self.dispatcher.select_account_for(
            provider=provider,
            model=model,
            affinity_hint=self.affinity_hint,
            quota_observations=self.quota_observations,
            pool_observations=self.pool_observations,
            now=utc_now_iso(),
        )

    # --- prepare ------------------------------------------------------------

    def prepare(
        self,
        *,
        base_request: WorkerRequest,
        provider: str,
        model: str,
    ) -> tuple[WorkerRequest, GatewayDispatchResult, ExecutionDisposition, str | None]:
        """Prepare the gateway execution boundary.

        Returns ``(augmented_request, gateway_evidence, disposition, bridge_session_id)``.

        The orchestrator inspects ``disposition``:

        * :data:`ExecutionDisposition.GATEWAY_READY` → invoke worker
          with the augmented request.
        * :data:`ExecutionDisposition.SAFE_DIRECT_FALLBACK` → invoke
          worker with the augmented request (it has *no* gateway env
          vars set, so the worker runs DIRECT).
        * :data:`ExecutionDisposition.GATEWAY_BLOCKED` → do not invoke
          the worker; record the typed disposition.
        * :data:`ExecutionDisposition.AMBIGUOUS_FORWARDING` →
          unreachable at prepare time (would require a prior launch).
        * :data:`ExecutionDisposition.DIRECT` → gateway disabled.

        This function NEVER makes a second semantic model request.
        """

        if not self.frozen_gateway.enabled:
            _, gateway_evidence = _legacy_prepare_dispatch_decision(
                base_request=base_request,
                frozen_gateway=self.frozen_gateway,
                provider=provider,
                model=model,
            )
            clean_env = _sanitize_base_env(
                dict(base_request.env) if base_request.env is not None else {},
                frozen_gateway=self.frozen_gateway,
            )
            return (
                _sanitized_worker_request(
                    base_request,
                    frozen_gateway=self.frozen_gateway,
                    env=clean_env,
                ),
                gateway_evidence,
                ExecutionDisposition.DIRECT,
                None,
            )

        if self.frozen_gateway.kind != GatewayKind.OMNIROUTE:
            raise GatewayManifestError(
                f"unsupported frozen gateway kind: {self.frozen_gateway.kind.value}"
            )

        # Store original semantic identity for evidence (never from selection_reason)
        self._requested_provider = provider
        self._requested_model = model

        selection = self.select_account(provider=provider, model=model)
        # Preserve configured authorization env-var name through freeze/reconstruct
        auth_env_var = getattr(
            self.frozen_gateway, "authorization_env_var", GATEWAY_AUTHORIZATION_ENV_DEFAULT
        )
        bridge = build_loopback_bridge_session(
            endpoint=self.endpoint,
            requested_account_id=selection.selected.account_id,
            provider=selection.selected.provider,
            authorization_env_var=auth_env_var,
        )
        bridge.start()
        self.bridge = bridge

        # Boundary-owned scrub: strip inherited gateway vars and the
        # configured authorization secret reference (and the default
        # ``OMNIROUTE_API_KEY``) so a base_request carrying the secret
        # value can never survive into the augmented worker env. Then
        # inject only the approved non-secret gateway values for this
        # dispatch.
        base_env_raw = dict(base_request.env) if base_request.env is not None else {}
        augmented_env = _sanitize_base_env(base_env_raw, frozen_gateway=self.frozen_gateway)
        augmented_env[GATEWAY_ENDPOINT_ENV] = self.endpoint
        augmented_env[GATEWAY_CONNECTION_ENV] = selection.selected.account_id
        augmented_env[GATEWAY_AUTHORIZATION_ENV] = auth_env_var
        augmented_env[GATEWAY_FROZEN_DIGEST_ENV] = self.frozen_gateway.digest
        augmented_env[GATEWAY_SELECTION_REASON_ENV] = selection.selection_reason
        augmented_env[GATEWAY_PROVIDER_ENV] = "orch_omniroute"
        augmented_env[GATEWAY_LOOPBACK_ENV] = bridge.loopback_url
        # Supply the fixed non-secret sentinel so Codex can satisfy its
        # env_key requirement on the loopback hop without ever seeing the
        # real OmniRoute token.
        augmented_env[_BRIDGE_SENTINEL_ENV] = _BRIDGE_SENTINEL_VALUE
        augmented = _sanitized_worker_request(
            base_request,
            frozen_gateway=self.frozen_gateway,
            env=augmented_env,
        )
        self.augmented_request = augmented
        self.selection_result = selection

        gateway_evidence = self._build_evidence(selection=selection, status="READY")

        context = GatewayExecutionContext(
            frozen_digest=self.frozen_gateway.digest,
            endpoint=self.endpoint,
            requested_account_id=selection.selected.account_id,
            selection_reason=selection.selection_reason,
            provider=provider,
            model=model,
            bridge_loopback_url=bridge.loopback_url,
            bridge_session_id=bridge.session_id,
            direct_fallback_allowed=self.frozen_gateway.direct_fallback_allowed,
            forwarding_state=bridge.state.forwarding_state,
            any_request_forwarded=bridge.state.any_request_forwarded,
            request_count=bridge.state.request_count,
        )
        self.pre_launch_decision = context
        decision = decide_gateway_execution(context)
        return augmented, gateway_evidence, decision.disposition, bridge.session_id

    # --- finalize -----------------------------------------------------------

    def finalize(
        self,
        *,
        observed_account_id: str | None = None,
        account_mismatch: bool = False,
        worker_result: WorkerResult | None = None,
    ) -> tuple[ExecutionDisposition, GatewayDispatchResult | None, BridgeEvidence | None]:
        """Stop the bridge and return the post-launch disposition.

        The orchestrator calls this exactly once, immediately after the
        worker adapter returns.  The function performs no semantic
        work; it only captures the bridge evidence and resolves the
        post-launch disposition.
        """

        bridge = self.bridge
        if bridge is None:
            # Pre-launch preparation never produced a bridge: treat as
            # unchanged disposition.  The orchestrator decides what to
            # do based on the pre-launch disposition it already saw.
            return ExecutionDisposition.DIRECT, None, None

        bridge.stop()
        evidence = bridge.finalize()
        self.bridge_evidence = evidence

        # Sticky mismatch: if ANY request in the session reported a
        # positive mismatch, it remains sticky for the attempt.
        sticky_mismatch = evidence.sticky_mismatch
        if sticky_mismatch:
            account_mismatch = True
            observed_account_id = evidence.observed_account_id
        elif account_mismatch:
            observed_account_id = observed_account_id or evidence.observed_account_id
        elif (
            evidence.observed_account_id is not None
            and evidence.latest_observed_account_id is not None
        ):
            # Use latest observed if no sticky mismatch caller flag
            observed_account_id = observed_account_id or evidence.latest_observed_account_id

        # Preserve exact provider/model chosen by Orch/C07 — never from
        # selection_reason. Fall back to stored original if available.
        selection_provider = self._requested_provider or (
            self.selection_result.selected.provider
            if self.selection_result is not None
            else "codex"
        )
        selection_model = self._requested_model or (
            self.augmented_request.model if self.augmented_request is not None else ""
        )
        selection = self.selection_result
        selection_reason = selection.selection_reason if selection is not None else ""
        context = GatewayExecutionContext(
            frozen_digest=self.frozen_gateway.digest,
            endpoint=self.endpoint,
            requested_account_id=(selection.selected.account_id if selection is not None else None),
            selection_reason=selection_reason,
            provider=selection_provider,
            model=selection_model,
            bridge_loopback_url=bridge.loopback_url,
            bridge_session_id=bridge.session_id,
            direct_fallback_allowed=self.frozen_gateway.direct_fallback_allowed,
            account_mismatch_detected=account_mismatch,
            bridge_evidence=evidence,
            forwarding_state=evidence.forwarding_state,
            last_error=evidence.error_message or evidence.last_error_message,
            any_request_forwarded=evidence.any_request_forwarded,
            request_count=evidence.request_count,
        )
        decision = decide_gateway_execution(context)
        # Forwarding invariant: if ANY request was forwarded and worker
        # later fails ambiguously, no automatic direct replay. Upgrade
        # to AMBIGUOUS_FORWARDING when worker failed after forwarding.
        if (
            worker_result is not None
            and worker_result.status != AttemptStatus.SUCCESS
            and evidence.any_request_forwarded
            and not account_mismatch
            and decision.disposition == ExecutionDisposition.GATEWAY_READY
        ):
            # Worker failed after at least one forwarded request — treat
            # as ambiguous infrastructure uncertainty.
            amb_context = GatewayExecutionContext(
                frozen_digest=self.frozen_gateway.digest,
                endpoint=self.endpoint,
                requested_account_id=(
                    selection.selected.account_id if selection is not None else None
                ),
                selection_reason=selection_reason,
                provider=selection_provider,
                model=selection_model,
                bridge_loopback_url=bridge.loopback_url,
                bridge_session_id=bridge.session_id,
                direct_fallback_allowed=self.frozen_gateway.direct_fallback_allowed,
                account_mismatch_detected=False,
                bridge_evidence=evidence,
                forwarding_state=BridgeForwardingState.REQUEST_FAILED,
                last_error=worker_result.error
                or evidence.error_message
                or evidence.last_error_message,
                any_request_forwarded=True,
                request_count=evidence.request_count,
            )
            decision = decide_gateway_execution(amb_context)
            gateway_evidence = self._build_evidence(
                selection=self.selection_result,
                status=decision.disposition.value,
                bridge_evidence=evidence,
                account_mismatch=False,
                observed_account_id=observed_account_id,
            )
            return decision.disposition, gateway_evidence, evidence
        gateway_evidence = self._build_evidence(
            selection=self.selection_result,
            status=decision.disposition.value,
            bridge_evidence=evidence,
            account_mismatch=account_mismatch,
            observed_account_id=observed_account_id,
        )
        return decision.disposition, gateway_evidence, evidence

    # --- helpers ------------------------------------------------------------

    def _build_evidence(
        self,
        *,
        selection: AccountSelectionResult | None,
        status: str,
        bridge_evidence: BridgeEvidence | None = None,
        account_mismatch: bool = False,
        observed_account_id: str | None = None,
    ) -> GatewayDispatchResult:
        if selection is None:
            raise GatewayManifestError("cannot build evidence without selection")
        # Preserve exact semantic identity — use stored original if available
        requested_provider = self._requested_provider or selection.selected.provider
        requested_model = self._requested_model or self._resolved_model()
        gateway_selection = GatewaySelection(
            requested_account_id=selection.selected.account_id,
            provider=requested_provider,
            observed_account_id=observed_account_id,
            selection_reason=selection.selection_reason,
            selected_at=utc_now_iso(),
            pool_id=selection.selected.pool_id,
            free_capacity=selection.selected.free_capacity,
            affinity_retained=selection.affinity_retained,
            affinity_broken=selection.affinity_broken,
            affinity_break_reason=selection.affinity_break_reason,
        )
        evidence_payload: dict[str, Any] = {
            "frozen_digest": self.frozen_gateway.digest,
            "gateway_used": True,
            "execution_status": status,
            "selection_reason": selection.selection_reason,
            "requested_provider": requested_provider,
            "requested_model": requested_model,
            "bridge_session_id": (
                bridge_evidence.session_id if bridge_evidence is not None else None
            ),
            "bridge_forwarding_state": (
                bridge_evidence.forwarding_state.value
                if bridge_evidence is not None
                else "FORWARDING_NOT_STARTED"
            ),
        }
        if bridge_evidence is not None:
            evidence_payload["bridge_status_code"] = bridge_evidence.status_code
            evidence_payload["bridge_latency_ms"] = bridge_evidence.latency_ms
            evidence_payload["request_count"] = bridge_evidence.request_count
            evidence_payload["any_request_forwarded"] = bridge_evidence.any_request_forwarded
            evidence_payload["completed_request_count"] = bridge_evidence.completed_request_count
            evidence_payload["failed_request_count"] = bridge_evidence.failed_request_count
            evidence_payload["sticky_mismatch"] = bridge_evidence.sticky_mismatch
            if bridge_evidence.latest_observed_account_id is not None:
                evidence_payload["latest_observed_account_id"] = (
                    bridge_evidence.latest_observed_account_id
                )
            if bridge_evidence.request_count:
                evidence_payload["request_forwarded"] = bridge_evidence.any_request_forwarded
            if bridge_evidence.error_kind is not None:
                evidence_payload["bridge_error_kind"] = bridge_evidence.error_kind.value
            if bridge_evidence.error_message is not None:
                evidence_payload["bridge_error_message"] = bridge_evidence.error_message
            if bridge_evidence.first_error_kind is not None:
                evidence_payload["first_bridge_error_kind"] = bridge_evidence.first_error_kind.value
            if bridge_evidence.first_error_message is not None:
                evidence_payload["first_bridge_error_message"] = bridge_evidence.first_error_message
            if bridge_evidence.last_error_kind is not None:
                evidence_payload["last_bridge_error_kind"] = bridge_evidence.last_error_kind.value
            if bridge_evidence.last_error_message is not None:
                evidence_payload["last_bridge_error_message"] = bridge_evidence.last_error_message
            evidence_payload["bridge_serialized"] = serialize_bridge_evidence(bridge_evidence)
        if account_mismatch:
            evidence_payload["account_mismatch"] = True
        if observed_account_id is not None:
            evidence_payload["observed_account_id"] = observed_account_id
        # request_forwarded semantics for the attempt: true if ANY request forwarded
        if bridge_evidence is not None:
            evidence_payload["request_forwarded"] = bool(bridge_evidence.any_request_forwarded)
        dispatch_target = GatewayDispatchTarget(
            selection=gateway_selection,
            health=self.selection_result.health if self.selection_result else None,
            quota=self.selection_result.quota if self.selection_result else None,
            request_identifier=f"sel-{utc_now_iso()}",
        )
        # Determine typed error: mismatch has priority, otherwise map bridge
        # evidence (status codes, transport failures) into the C10 taxonomy.
        error: GatewayError | None = None
        if account_mismatch:
            error = GatewayError(
                kind=GatewayErrorKind.ACCOUNT_MISMATCH,
                message="gateway account mismatch detected (sticky)",
                recoverable=False,
                request_forwarded=bool(
                    bridge_evidence.any_request_forwarded if bridge_evidence else False
                ),
            )
        elif bridge_evidence is not None:
            error = _gateway_error_from_bridge(bridge_evidence, status)
        return GatewayDispatchResult(
            success=error is None,
            kind=GatewayKind.OMNIROUTE,
            requested_provider=requested_provider,
            requested_model=requested_model,
            evidence=evidence_payload,
            error=error,
            target=dispatch_target,
        )

    def _resolved_model(self) -> str:
        if self._requested_model is not None:
            return self._requested_model
        if self.augmented_request is not None:
            return self.augmented_request.model
        return ""


@dataclass
class GatewayExecutionOutcome:
    """The complete, single-attempt outcome of a gateway-aware dispatch.

    The orchestrator receives one outcome per worker attempt and
    persists it on the DispatchEvidence row.
    """

    worker_request: WorkerRequest
    worker_result: WorkerResult | None
    gateway_evidence: GatewayDispatchResult | None
    disposition: ExecutionDisposition
    bridge_evidence: BridgeEvidence | None = None
    bridge_session_id: str | None = None
    pre_launch_disposition: ExecutionDisposition | None = None
    worker_invoked: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "disposition": self.disposition.value,
            "pre_launch_disposition": (
                self.pre_launch_disposition.value
                if self.pre_launch_disposition is not None
                else None
            ),
            "worker_invoked": self.worker_invoked,
            "bridge_session_id": self.bridge_session_id,
        }


def _build_blocked_evidence(
    *,
    provider: str,
    model: str,
    reason: str,
    error_kind: GatewayErrorKind = GatewayErrorKind.UNAVAILABLE,
    gateway_used: bool = False,
    request_forwarded: bool = False,
    extra_evidence: dict[str, Any] | None = None,
) -> GatewayDispatchResult:
    evidence: dict[str, Any] = {
        "execution_status": ExecutionDisposition.GATEWAY_BLOCKED.value,
        "gateway_used": gateway_used,
        "request_forwarded": request_forwarded,
        "reason": reason,
        "requested_provider": provider,
        "requested_model": model,
    }
    if extra_evidence:
        evidence.update(extra_evidence)
    return GatewayDispatchResult(
        success=False,
        kind=GatewayKind.OMNIROUTE,
        requested_provider=provider,
        requested_model=model,
        evidence=evidence,
        error=GatewayError(
            kind=error_kind,
            message=reason,
            recoverable=False,
            request_forwarded=request_forwarded,
        ),
    )


def _build_safe_fallback_evidence(
    *,
    provider: str,
    model: str,
    fallback_reason: str,
) -> GatewayDispatchResult:
    return GatewayDispatchResult(
        success=True,
        kind=GatewayKind.OMNIROUTE,
        requested_provider=provider,
        requested_model=model,
        evidence={
            "execution_status": ExecutionDisposition.SAFE_DIRECT_FALLBACK.value,
            "gateway_used": False,
            "fallback": True,
            "fallback_reason": fallback_reason,
            "request_forwarded": False,
            "requested_provider": provider,
            "requested_model": model,
        },
    )


def run_worker_with_gateway_boundary(
    *,
    frozen_gateway: FrozenGatewayConfig | None,
    endpoint: str | None,
    base_request: WorkerRequest,
    provider: str,
    model: str,
    run_adapter: Any,
    adapter_supports_gateway: bool,
    accounts: tuple[GatewayAccount, ...] = (),
    health_state: GatewayHealthState | None = None,
    affinity_hint: AffinityHint | None = None,
    quota_observations: Iterable[GatewayQuotaObservation] = (),
    pool_observations: Iterable[GatewayQuotaPoolView] = (),
) -> GatewayExecutionOutcome:

    # Case 1: No gateway or adapter does not support gateway transport
    if frozen_gateway is None or not frozen_gateway.enabled or not adapter_supports_gateway:
        # Adapter does not advertise C10 gateway transport capability.
        # When direct fallback is forbidden, this is a typed infrastructure
        # blockage with no worker invocation.
        if (
            frozen_gateway is not None
            and frozen_gateway.enabled
            and not frozen_gateway.direct_fallback_allowed
        ):
            blocked_evidence = _build_blocked_evidence(
                provider=provider,
                model=model,
                reason="adapter_gateway_unsupported",
                error_kind=GatewayErrorKind.UNAVAILABLE,
                gateway_used=False,
                request_forwarded=False,
                extra_evidence={
                    "adapter_supports_gateway": False,
                    "direct_fallback_allowed": False,
                },
            )
            return GatewayExecutionOutcome(
                worker_request=base_request,
                worker_result=None,
                gateway_evidence=blocked_evidence,
                disposition=ExecutionDisposition.GATEWAY_BLOCKED,
                worker_invoked=False,
                pre_launch_disposition=ExecutionDisposition.GATEWAY_BLOCKED,
            )
        # Fallback allowed or gateway disabled: execute direct exactly once,
        # without pretending gateway participated. For adapter-unsupported
        # with fallback allowed, emit SAFE_DIRECT_FALLBACK evidence.
        if frozen_gateway is not None and frozen_gateway.enabled and not adapter_supports_gateway:
            # Unsupported adapter but fallback allowed — direct execution
            fallback_evidence = _build_safe_fallback_evidence(
                provider=provider,
                model=model,
                fallback_reason="adapter_gateway_unsupported",
            )
            # Boundary-owned scrub strips gateway-control vars AND the
            # configured authorization reference from the env before any
            # worker launch.
            clean_env = _sanitize_base_env(
                dict(base_request.env) if base_request.env is not None else {},
                frozen_gateway=frozen_gateway,
            )
            clean_request = _sanitized_worker_request(
                base_request,
                frozen_gateway=frozen_gateway,
                env=clean_env if clean_env else {},
            )
            worker_result = run_adapter(clean_request)
            return GatewayExecutionOutcome(
                worker_request=clean_request,
                worker_result=worker_result,
                gateway_evidence=fallback_evidence,
                disposition=ExecutionDisposition.SAFE_DIRECT_FALLBACK,
                worker_invoked=True,
                pre_launch_disposition=ExecutionDisposition.SAFE_DIRECT_FALLBACK,
            )
        # Pure DIRECT (gateway disabled). The legacy helper only constructs
        # DIRECT evidence on this branch — it does not make gateway/bridge/
        # selection calls and returns ``base_request`` unchanged. The boundary
        # MUST route the worker invocation through the same single-source-of-
        # truth sanitizer so stale gateway/secret values inherited in
        # ``base_request.env`` cannot survive into the semantic launch.
        clean_env = _sanitize_base_env(
            dict(base_request.env) if base_request.env is not None else {},
            frozen_gateway=frozen_gateway,
        )
        clean_request = _sanitized_worker_request(
            base_request,
            frozen_gateway=frozen_gateway,
            env=clean_env,
        )
        _, gateway_evidence = _legacy_prepare_dispatch_decision(
            base_request=base_request,
            frozen_gateway=frozen_gateway,
            provider=provider,
            model=model,
            omniroute_endpoint=endpoint,
            accounts=accounts,
        )
        worker_result = run_adapter(clean_request)
        return GatewayExecutionOutcome(
            worker_request=clean_request,
            worker_result=worker_result,
            gateway_evidence=gateway_evidence,
            disposition=ExecutionDisposition.DIRECT,
            worker_invoked=True,
            pre_launch_disposition=ExecutionDisposition.DIRECT,
        )

    # At this point: gateway enabled and adapter supports gateway.

    # Case 2: Missing endpoint while gateway-enabled and gateway-capable
    if not endpoint:
        if not frozen_gateway.direct_fallback_allowed:
            blocked_evidence = _build_blocked_evidence(
                provider=provider,
                model=model,
                reason="endpoint_missing",
                error_kind=GatewayErrorKind.UNAVAILABLE,
                gateway_used=False,
                request_forwarded=False,
                extra_evidence={
                    "endpoint_missing": True,
                    "direct_fallback_allowed": False,
                    "frozen_digest": frozen_gateway.digest,
                },
            )
            return GatewayExecutionOutcome(
                worker_request=base_request,
                worker_result=None,
                gateway_evidence=blocked_evidence,
                disposition=ExecutionDisposition.GATEWAY_BLOCKED,
                worker_invoked=False,
                pre_launch_disposition=ExecutionDisposition.GATEWAY_BLOCKED,
            )
        # Fallback allowed: one DIRECT execution, SAFE_DIRECT_FALLBACK
        fallback_evidence = _build_safe_fallback_evidence(
            provider=provider,
            model=model,
            fallback_reason="endpoint_missing",
        )
        # Preserve frozen_digest for correlation but no gateway claim
        fallback_evidence.evidence["frozen_digest"] = frozen_gateway.digest
        # Boundary-owned scrub: strip gateway vars and configured
        # authorization reference for a true DIRECT launch.
        clean_env = _sanitize_base_env(
            dict(base_request.env) if base_request.env is not None else {},
            frozen_gateway=frozen_gateway,
        )
        clean_request = _sanitized_worker_request(
            base_request,
            frozen_gateway=frozen_gateway,
            env=clean_env if clean_env else {},
        )
        worker_result = run_adapter(clean_request)
        return GatewayExecutionOutcome(
            worker_request=clean_request,
            worker_result=worker_result,
            gateway_evidence=fallback_evidence,
            disposition=ExecutionDisposition.SAFE_DIRECT_FALLBACK,
            worker_invoked=True,
            pre_launch_disposition=ExecutionDisposition.SAFE_DIRECT_FALLBACK,
        )

    try:
        boundary = GatewayExecutionBoundary.from_frozen_config(
            frozen_gateway=frozen_gateway,
            endpoint=endpoint,
            accounts=accounts,
            health_state=health_state,
            affinity_hint=affinity_hint,
            quota_observations=quota_observations,
            pool_observations=pool_observations,
        )
        augmented, gateway_evidence, pre_launch, bridge_session_id = boundary.prepare(
            base_request=base_request,
            provider=provider,
            model=model,
        )
    except GatewayManifestError as exc:
        # Pre-launch configuration/selection failure before any forwarding
        error_message = str(exc)
        # Preserve actual reason: classify by message content where possible
        lower_msg = error_message.lower()
        if "quota" in lower_msg or "red line" in lower_msg:
            error_kind = GatewayErrorKind.QUOTA_UNAVAILABLE
            reason = "quota_unavailable"
        elif "cooldown" in lower_msg or "unavailable" in lower_msg or "health" in lower_msg:
            error_kind = GatewayErrorKind.ACCOUNT_UNAVAILABLE
            reason = "account_unavailable"
        elif "permit" in lower_msg or "configuration defect" in lower_msg:
            error_kind = GatewayErrorKind.ACCOUNT_UNAVAILABLE
            reason = "account_unavailable"
        else:
            error_kind = GatewayErrorKind.ACCOUNT_UNAVAILABLE
            reason = "pre_launch_configuration_failure"
        # Build accurate evidence for blocked path
        if not frozen_gateway.direct_fallback_allowed:
            blocked_evidence = _build_blocked_evidence(
                provider=provider,
                model=model,
                reason=reason,
                error_kind=error_kind,
                gateway_used=False,
                request_forwarded=False,
                extra_evidence={
                    "pre_launch_error": error_message,
                    "frozen_digest": frozen_gateway.digest,
                },
            )
            return GatewayExecutionOutcome(
                worker_request=base_request,
                worker_result=None,
                gateway_evidence=blocked_evidence,
                disposition=ExecutionDisposition.GATEWAY_BLOCKED,
                worker_invoked=False,
                pre_launch_disposition=ExecutionDisposition.GATEWAY_BLOCKED,
            )
        # Fallback allowed: run the *original* base_request DIRECT exactly
        # once with zero gateway augmentation. Do NOT call legacy gateway
        # preparation again (that would re-select an account/env).
        # Boundary-owned scrub: strip gateway vars and configured
        # authorization reference for a true DIRECT launch.
        clean_env = _sanitize_base_env(
            dict(base_request.env) if base_request.env is not None else {},
            frozen_gateway=frozen_gateway,
        )
        # Preserve non-gateway env but ensure no gateway augmentation remains
        clean_request = _sanitized_worker_request(
            base_request,
            frozen_gateway=frozen_gateway,
            env=clean_env if clean_env else {},
        )
        fallback_evidence = _build_safe_fallback_evidence(
            provider=provider,
            model=model,
            fallback_reason=reason,
        )
        # Preserve frozen_digest for correlation and pre_launch_error for audit
        fallback_evidence.evidence["frozen_digest"] = frozen_gateway.digest
        fallback_evidence.evidence["pre_launch_error"] = error_message
        # Ensure requested identity is exact (builder already does)
        fallback_evidence.evidence["requested_provider"] = provider
        fallback_evidence.evidence["requested_model"] = model
        # Ensure explicit fields required by spec
        fallback_evidence.evidence["gateway_used"] = False
        fallback_evidence.evidence["fallback"] = True
        fallback_evidence.evidence["request_forwarded"] = False
        fallback_evidence.evidence["target"] = None  # No account served direct fallback
        worker_result = run_adapter(clean_request)
        return GatewayExecutionOutcome(
            worker_request=clean_request,
            worker_result=worker_result,
            gateway_evidence=fallback_evidence,
            disposition=ExecutionDisposition.SAFE_DIRECT_FALLBACK,
            worker_invoked=True,
            pre_launch_disposition=ExecutionDisposition.SAFE_DIRECT_FALLBACK,
        )

    if pre_launch == ExecutionDisposition.GATEWAY_BLOCKED:
        return GatewayExecutionOutcome(
            worker_request=augmented,
            worker_result=None,
            gateway_evidence=gateway_evidence,
            disposition=ExecutionDisposition.GATEWAY_BLOCKED,
            bridge_session_id=bridge_session_id,
            pre_launch_disposition=pre_launch,
            worker_invoked=False,
        )

    worker_result = run_adapter(augmented)
    post_disposition, post_evidence, bridge_evidence = boundary.finalize(
        worker_result=worker_result
    )
    # Forwarding invariant: if ANY request was forwarded and worker later
    # fails ambiguously, no automatic direct replay — preserve AMBIGUOUS
    # disposition without falling back.
    # The boundary already captures this via BridgeEvidence.any_request_forwarded
    # and decide_gateway_execution logic.

    # If bridge detected sticky mismatch, ensure post_disposition is BLOCKED
    # (decide_gateway_execution handles this via account_mismatch flag).

    return GatewayExecutionOutcome(
        worker_request=augmented,
        worker_result=worker_result,
        gateway_evidence=post_evidence if post_evidence is not None else gateway_evidence,
        disposition=post_disposition,
        bridge_evidence=bridge_evidence,
        bridge_session_id=bridge_session_id,
        pre_launch_disposition=pre_launch,
        worker_invoked=True,
    )


__all__ = [
    "GATEWAY_AUTHORIZATION_ENV",
    "GATEWAY_AUTHORIZATION_ENV_DEFAULT",
    "GATEWAY_CONNECTION_ENV",
    "GATEWAY_ENDPOINT_ENV",
    "GATEWAY_FROZEN_DIGEST_ENV",
    "GATEWAY_LOOPBACK_ENV",
    "GATEWAY_PROVIDER_ENV",
    "GATEWAY_SELECTION_REASON_ENV",
    "GatewayExecutionBoundary",
    "GatewayExecutionOutcome",
    "GatewayLiveState",
    "GatewayLiveStateProvider",
    "run_worker_with_gateway_boundary",
]
