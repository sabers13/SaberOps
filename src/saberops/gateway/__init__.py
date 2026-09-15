"""C10 — OmniRoute Gateway & Governed Free Capacity.

C10 introduces an *optional* provider/account gateway that sits beneath
Orch's semantic dispatch. The architecture invariant is:

    Orch selects provider/model -> C07 hard policy -> optional gateway
    -> equivalent account/connection only

A gateway module never chooses another provider/model/tier/role. It
only chooses an equivalent account/connection for the *already selected*
semantic candidate. C08 remains owner of tool transport; OmniRoute
Auto/Combo/Fusion/Pipeline/Context-Relay/MCP facilities are *not*
adopted by this module.

Public surface:

* :class:`GatewayKind`, :class:`GatewayStatus`, :class:`GatewayAccount`,
  :class:`GatewayHealth`, :class:`GatewayQuotaObservation`,
  :class:`GatewaySelection`, :class:`GatewayDispatchTarget`,
  :class:`GatewayDispatchResult`, :class:`GatewayError`,
  :class:`GatewayErrorKind`, :class:`FrozenGatewayConfig`,
  :func:`freeze_gateway_config`.
* :class:`GatewayRegistry` and :class:`DirectGateway`.
* :class:`OmniRouteGateway`, :class:`OmniRouteBackendConfig`,
  :class:`OmniRouteDispatchRequest`, :func:`freeze_omniroute_config`.
* :class:`GatewayDispatcher` for the workflow-layer seam.
* :class:`GatewayTransport` and :class:`HttpxGatewayTransport` for the
  injectable HTTP seam.

Secret material (bearer tokens, API keys, OAuth credentials) is *never*
persisted, logged, or emitted in evidence, exceptions, frozen config,
or telemetry. Configuration references secrets by environment-variable
name; the value is resolved at invocation time and dropped immediately.
"""

from saberops.gateway.accounts import (
    AccountSelectionInputs,
    AccountSelectionResult,
    break_affinity,
    select_account,
)
from saberops.gateway.affinity import (
    AffinityBreakEvidence,
    AffinityHint,
    matches_target,
    record_break,
    should_break,
)
from saberops.gateway.boundary import (
    GatewayExecutionBoundary,
    GatewayExecutionOutcome,
    GatewayLiveState,
    GatewayLiveStateProvider,
    run_worker_with_gateway_boundary,
)
from saberops.gateway.bridge import (
    BRIDGE_VERSION,
    BridgeErrorKind,
    BridgeEvidence,
    BridgeForwardingState,
    BridgeSession,
    build_loopback_bridge_session,
    parse_provider_config_overrides,
    scrub_request_headers,
    scrub_response_headers,
    serialize_bridge_evidence,
)
from saberops.gateway.client import (
    GatewayHttpRequest,
    GatewayHttpResponse,
    GatewayTransport,
    GatewayTransportError,
    HttpxGatewayTransport,
)
from saberops.gateway.contracts import (
    FrozenGatewayConfig,
    GatewayAccount,
    GatewayDispatchResult,
    GatewayDispatchTarget,
    GatewayError,
    GatewayErrorKind,
    GatewayHealth,
    GatewayKind,
    GatewayManifestError,
    GatewayQuotaFreshness,
    GatewayQuotaObservation,
    GatewaySelection,
    GatewayStatus,
    freeze_gateway_config,
    reconstruct_frozen_gateway_config,
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
    build_direct_evidence,
    deserialize_dispatch_evidence,
    freeze_dispatcher_config,
    prepare_dispatch_decision,
    serialize_dispatch_evidence,
)
from saberops.gateway.execution import (
    ExecutionDisposition,
    GatewayExecutionContext,
    GatewayExecutionDecision,
    decide_gateway_execution,
)
from saberops.gateway.health import (
    GatewayHealthState,
    healthy_accounts,
    mark_health,
    utc_now_iso,
)
from saberops.gateway.omniroute import (
    DEFAULT_TIMEOUT_SECONDS,
    OMNIROUTE_ENV_VAR,
    OmniRouteBackendConfig,
    OmniRouteDispatchRequest,
    OmniRouteGateway,
    freeze_omniroute_config,
)
from saberops.gateway.quota import (
    RED_LINE_PERCENT,
    GatewayQuotaPoolView,
    aggregate_pool,
    is_below_red_line,
    normalize_observation,
)
from saberops.gateway.registry import (
    DirectGateway,
    GatewayFactory,
    GatewayRegistry,
    GatewayRegistryEntry,
    build_direct_dispatch,
    resolve_dispatch_kind,
)

__all__ = [
    "BRIDGE_VERSION",
    "DEFAULT_TIMEOUT_SECONDS",
    "ExecutionDisposition",
    "GATEWAY_AUTHORIZATION_ENV",
    "GATEWAY_AUTHORIZATION_ENV_DEFAULT",
    "GATEWAY_CONNECTION_ENV",
    "GATEWAY_ENDPOINT_ENV",
    "GATEWAY_FROZEN_DIGEST_ENV",
    "GATEWAY_LOOPBACK_ENV",
    "GATEWAY_PROVIDER_ENV",
    "GATEWAY_SELECTION_REASON_ENV",
    "GatewayExecutionBoundary",
    "GatewayExecutionContext",
    "GatewayExecutionDecision",
    "GatewayExecutionOutcome",
    "GatewayLiveState",
    "GatewayLiveStateProvider",
    "OMNIROUTE_ENV_VAR",
    "RED_LINE_PERCENT",
    "AccountSelectionInputs",
    "AccountSelectionResult",
    "AffinityBreakEvidence",
    "AffinityHint",
    "BridgeErrorKind",
    "BridgeEvidence",
    "BridgeForwardingState",
    "BridgeSession",
    "DirectGateway",
    "FrozenGatewayConfig",
    "GatewayAccount",
    "GatewayDispatcher",
    "GatewayDispatchResult",
    "GatewayDispatchTarget",
    "GatewayError",
    "GatewayErrorKind",
    "GatewayFactory",
    "GatewayHealth",
    "GatewayHealthState",
    "GatewayHttpRequest",
    "GatewayHttpResponse",
    "GatewayKind",
    "GatewayManifestError",
    "GatewayQuotaFreshness",
    "GatewayQuotaObservation",
    "GatewayQuotaPoolView",
    "GatewayRegistry",
    "GatewayRegistryEntry",
    "GatewaySelection",
    "GatewayStatus",
    "GatewayTransport",
    "GatewayTransportError",
    "HttpxGatewayTransport",
    "OmniRouteBackendConfig",
    "OmniRouteDispatchRequest",
    "OmniRouteGateway",
    "break_affinity",
    "build_direct_dispatch",
    "build_direct_evidence",
    "build_loopback_bridge_session",
    "decide_gateway_execution",
    "deserialize_dispatch_evidence",
    "freeze_dispatcher_config",
    "freeze_gateway_config",
    "freeze_omniroute_config",
    "healthy_accounts",
    "is_below_red_line",
    "mark_health",
    "matches_target",
    "normalize_observation",
    "aggregate_pool",
    "parse_provider_config_overrides",
    "prepare_dispatch_decision",
    "reconstruct_frozen_gateway_config",
    "record_break",
    "resolve_dispatch_kind",
    "run_worker_with_gateway_boundary",
    "scrub_request_headers",
    "scrub_response_headers",
    "select_account",
    "serialize_bridge_evidence",
    "serialize_dispatch_evidence",
    "should_break",
    "utc_now_iso",
]
