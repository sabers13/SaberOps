"""Top-level C10 gateway dispatch surface.

This module is the integration seam: it composes account selection,
health, cooldown, quota, and affinity into a single callable that the
workflow layer invokes *after* C07 hard policy has admitted the
candidate. The dispatch surface never makes a semantic routing decision
on its own; it only chooses an equivalent account/connection and reports
the result back to Orch.

The surface is intentionally small and free of provider-specific code.
All gateway knowledge lives in :mod:`orchestrator_mvp.gateway.omniroute`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from orchestrator_mvp.gateway.accounts import AccountSelectionResult
from orchestrator_mvp.gateway.affinity import AffinityHint
from orchestrator_mvp.gateway.client import GatewayTransport
from orchestrator_mvp.gateway.contracts import (
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
from orchestrator_mvp.gateway.health import (
    GatewayHealthState,
    utc_now_iso,
)
from orchestrator_mvp.gateway.omniroute import (
    OMNIROUTE_ENV_VAR,
    OmniRouteBackendConfig,
    OmniRouteDispatchRequest,
    OmniRouteGateway,
    freeze_omniroute_config,
)
from orchestrator_mvp.gateway.quota import GatewayQuotaPoolView
from orchestrator_mvp.gateway.registry import (
    DirectGateway,
    GatewayRegistry,
    GatewayRegistryEntry,
    build_direct_dispatch,
    resolve_dispatch_kind,
)
from orchestrator_mvp.models import WorkerRequest


@dataclass
class GatewayDispatcher:
    """Composition root for C10 gateway dispatch.

    The dispatcher owns the registry, the active frozen contract, the
    health store, and the current affinity hint. It exposes one method
    per dispatch kind; the workflow layer picks the appropriate method
    based on the candidate.
    """

    registry: GatewayRegistry
    frozen_config: FrozenGatewayConfig | None
    health: GatewayHealthState
    affinity_hint: AffinityHint | None = None
    omniroute: OmniRouteGateway | None = None

    @classmethod
    def from_frozen_config(
        cls,
        *,
        frozen_config: FrozenGatewayConfig | None,
        accounts: tuple[GatewayAccount, ...] = (),
        health: GatewayHealthState | None = None,
        omniroute_transport: Any | None = None,
        omniroute_endpoint: str | None = None,
    ) -> GatewayDispatcher:
        """Build a dispatcher from a frozen C10 gateway contract.

        Control flow:

        * ``frozen_config is None`` or ``enabled is False``
          → a valid DIRECT dispatcher (the first-class no-gateway path).
        * ``enabled is True`` and ``kind == OMNIROUTE``
          → a valid ``OmniRouteGateway`` plus registry plus health state.
        * Any other enabled kind
          → :class:`GatewayManifestError` (fail-closed configuration error).
        """
        health_state = health or GatewayHealthState.empty()
        if frozen_config is None or not frozen_config.enabled:
            return cls(
                registry=GatewayRegistry(),
                frozen_config=frozen_config,
                health=health_state,
            )
        if frozen_config.kind == GatewayKind.OMNIROUTE:
            if omniroute_transport is None or omniroute_endpoint is None:
                raise GatewayManifestError(
                    "OmniRoute is enabled but no transport was provided to the dispatcher"
                )
            omniroute_config = OmniRouteBackendConfig(
                endpoint=omniroute_endpoint,
                api_key_env=getattr(frozen_config, "authorization_env_var", OMNIROUTE_ENV_VAR),
                permitted_provider_models=frozen_config.permitted_provider_models,
                direct_fallback_allowed=frozen_config.direct_fallback_allowed,
                affinity=dict(frozen_config.affinity),
            )
            gateway = OmniRouteGateway(
                config=omniroute_config,
                accounts=tuple(accounts),
                transport=omniroute_transport,
                health=health_state,
            )
            registry = GatewayRegistry(
                [
                    GatewayRegistryEntry(
                        kind=GatewayKind.OMNIROUTE,
                        factory=OmniRouteGateway,
                        config_digest=frozen_config.digest,
                        enabled=True,
                    )
                ]
            )
            return cls(
                registry=registry,
                frozen_config=frozen_config,
                health=health_state,
                omniroute=gateway,
            )
        raise GatewayManifestError(f"unsupported frozen gateway kind: {frozen_config.kind.value}")

    # --- direct ------------------------------------------------------------

    def dispatch_direct(self, *, provider: str, model: str) -> GatewayDispatchResult:
        return DirectGateway(enabled=False).dispatch(provider=provider, model=model)

    # --- omniroute ---------------------------------------------------------

    def dispatch_omniroute(
        self,
        request: OmniRouteDispatchRequest,
        *,
        quota_observations: Iterable[GatewayQuotaObservation] = (),
        pool_observations: Iterable[GatewayQuotaPoolView] = (),
        affinity_hint: AffinityHint | None = None,
    ) -> GatewayDispatchResult:
        if self.omniroute is None or self.frozen_config is None:
            return GatewayDispatchResult(
                success=False,
                kind=GatewayKind.OMNIROUTE,
                requested_provider=request.provider,
                requested_model=request.model,
                evidence={"gateway_used": False},
                error=GatewayError(
                    kind=GatewayErrorKind.UNAVAILABLE,
                    message="OmniRoute gateway is not enabled for this run",
                    recoverable=True,
                ),
            )
        authorization = self.omniroute.resolve_authorization()
        if authorization is None:
            return GatewayDispatchResult(
                success=False,
                kind=GatewayKind.OMNIROUTE,
                requested_provider=request.provider,
                requested_model=request.model,
                evidence={"authorization_present": False},
                error=GatewayError(
                    kind=GatewayErrorKind.AUTH_FAILED,
                    message="OmniRoute API key is not configured in the runtime environment",
                    recoverable=True,
                ),
            )
        effective_hint = affinity_hint if affinity_hint is not None else self.affinity_hint
        now = utc_now_iso()
        # Affinity break is now owned solely by the gateway's single
        # selection implementation (OmniRouteGateway._selection_for_request).
        # Do not maintain a second divergent policy here; just thread the
        # hint through and let the gateway record/browse the break durably.
        result = self.omniroute.dispatch_with_authorization(
            request,
            authorization=authorization,
            quota_observations=quota_observations,
            pool_observations=pool_observations,
            now=now,
            affinity_hint=effective_hint,
        )
        return result

    # --- high-level helper --------------------------------------------------

    def dispatch(
        self,
        *,
        provider: str,
        model: str,
        messages: tuple[Mapping[str, Any], ...],
        affinity_hint: AffinityHint | None = None,
        quota_observations: Iterable[GatewayQuotaObservation] = (),
        pool_observations: Iterable[GatewayQuotaPoolView] = (),
        session_id: str | None = None,
    ) -> GatewayDispatchResult:
        """Dispatch the exact provider/model through the active gateway kind.

        The dispatch kind is decided by the frozen contract: if no
        gateway is enabled, the result is a typed ``DIRECT`` evidence
        row and the workflow layer proceeds with the existing direct
        adapter path.
        """
        kind = resolve_dispatch_kind(
            requested=self._requested_kind(),
            frozen=self.frozen_config,
        )
        if kind == GatewayKind.DIRECT:
            return self.dispatch_direct(provider=provider, model=model)
        if kind == GatewayKind.OMNIROUTE:
            return self.dispatch_omniroute(
                OmniRouteDispatchRequest(
                    provider=provider,
                    model=model,
                    messages=messages,
                    session_id=session_id,
                ),
                quota_observations=quota_observations,
                pool_observations=pool_observations,
                affinity_hint=affinity_hint,
            )
        raise GatewayManifestError(f"unsupported gateway kind: {kind.value}")

    def _requested_kind(self) -> GatewayKind:
        if self.frozen_config is None or not self.frozen_config.enabled:
            return GatewayKind.DIRECT
        return self.frozen_config.kind


    # --- local account selection (no HTTP) -------------------------------

    def select_account_for(
        self,
        *,
        provider: str,
        model: str,
        affinity_hint: AffinityHint | None = None,
        quota_observations: Iterable[GatewayQuotaObservation] = (),
        pool_observations: Iterable[GatewayQuotaPoolView] = (),
        now: str | None = None,
    ) -> AccountSelectionResult:
        """Select an account/connection locally without making an HTTP call.

        This is the C10-R1 dispatch seam used when the worker adapter
        (e.g. Codex / OpenCode / Cline / Antigravity) is the execution
        owner and the gateway is consulted only to pick the pinned
        account. The selected account is then handed to the worker via
        the bounded ``WorkerRequest.env`` interface; the actual model
        execution happens in the worker, never in the gateway backend.

        Returns a typed :class:`AccountSelectionResult`. Raises
        :class:`GatewayManifestError` for configuration defects (no
        enabled backend, no matching account, all accounts in cooldown,
        etc.).
        """
        if self.omniroute is None:
            raise GatewayManifestError(
                "OmniRoute gateway is not enabled for this dispatcher"
            )
        # The OmniRouteGateway's ``select`` ignores its own
        # ``affinity_hint`` attribute, so we thread the dispatcher's
        # affinity_hint through by temporarily setting it for this
        # call.  C10-R3's boundary always builds a fresh
        # GatewayDispatcher per dispatch, so mutating the attribute
        # is safe and scoped.
        original_hint = self.omniroute.affinity_hint
        try:
            self.omniroute.affinity_hint = affinity_hint
            return self.omniroute.select(
                provider=provider,
                model=model,
                quota_observations=quota_observations,
                pool_observations=pool_observations,
                now=now,
            )
        finally:
            self.omniroute.affinity_hint = original_hint


def _matching_quota(
    account_id: str,
    quota_observations: Iterable[GatewayQuotaObservation],
    pool_observations: Iterable[GatewayQuotaPoolView],
) -> GatewayQuotaObservation | GatewayQuotaPoolView | None:
    for view in pool_observations:
        if account_id in getattr(view, "member_account_ids", ()):
            return view
    for observation in quota_observations:
        if observation.account_id == account_id:
            return observation
    return None


class _LocalSelectionTransport:
    """A sentinel transport for the C10-R1 local-selection seam.

    The local selection path never invokes the transport; constructing an
    :class:`OmniRouteGateway` only requires a callable ``post_with_auth``
    attribute so the gateway can be used for :meth:`select` without ever
    performing a network call.
    """

    def post_with_auth(
        self,
        request: Any,
        *,
        authorization: str,
    ) -> Any:
        raise GatewayManifestError(
            "local gateway selection path never invokes the HTTP transport; "
            "configure an HTTP-capable transport for OmniRoute-backed execution"
        )


LocalSelectionTransport = _LocalSelectionTransport


GATEWAY_ENDPOINT_ENV = "ORCH_GATEWAY_ENDPOINT"
GATEWAY_CONNECTION_ENV = "ORCH_GATEWAY_CONNECTION"
GATEWAY_AUTHORIZATION_ENV = "ORCH_GATEWAY_AUTHORIZATION_ENV"
GATEWAY_AUTHORIZATION_ENV_DEFAULT = OMNIROUTE_ENV_VAR
GATEWAY_FROZEN_DIGEST_ENV = "ORCH_GATEWAY_FROZEN_DIGEST"
GATEWAY_SELECTION_REASON_ENV = "ORCH_GATEWAY_SELECTION_REASON"
GATEWAY_LOOPBACK_ENV = "ORCH_GATEWAY_LOOPBACK_URL"
GATEWAY_PROVIDER_ENV = "ORCH_GATEWAY_PROVIDER_ID"


def _augment_request_with_gateway(
    base_request: WorkerRequest,
    *,
    endpoint: str,
    account_id: str,
    authorization_env: str,
    frozen_digest: str,
    selection_reason: str,
) -> WorkerRequest:
    """Return a copy of ``base_request`` with gateway pinning env vars.

    The augmentation is bounded, non-secret, and never embeds credentials:
    the bearer token is referenced by env-var name only and is resolved
    by the worker at invocation time.
    """
    base_env = dict(base_request.env) if base_request.env is not None else {}
    base_env[GATEWAY_ENDPOINT_ENV] = endpoint
    base_env[GATEWAY_CONNECTION_ENV] = account_id
    base_env[GATEWAY_AUTHORIZATION_ENV] = authorization_env
    base_env[GATEWAY_FROZEN_DIGEST_ENV] = frozen_digest
    base_env[GATEWAY_SELECTION_REASON_ENV] = selection_reason
    return WorkerRequest(
        task=base_request.task,
        worktree_path=base_request.worktree_path,
        model=base_request.model,
        timeout_seconds=base_request.timeout_seconds,
        env=base_env,
        read_only=base_request.read_only,
        inactivity_timeout=base_request.inactivity_timeout,
        on_stdout=base_request.on_stdout,
        on_stderr=base_request.on_stderr,
        on_stall=base_request.on_stall,
        on_resume=base_request.on_resume,
        on_process_start=base_request.on_process_start,
        on_monitor_transition=base_request.on_monitor_transition,
        # C11-D: the gateway copy path must preserve the typed
        # execution fields it is supposed to carry -- never silently
        # reset the resolved effort or drop secret isolation.
        inherit_env_exclude=base_request.inherit_env_exclude,
        reasoning_effort=base_request.reasoning_effort,
    )


def _evidence_from_selection(
    *,
    selection_result: AccountSelectionResult,
    frozen_digest: str,
    provider: str,
    model: str,
    timestamp: str,
) -> GatewayDispatchResult:
    """Build a typed :class:`GatewayDispatchResult` from a local selection.

    The HTTP-backed OmniRoute dispatch would emit the receipt headers
    (``x-omniroute-selected-connection-id`` etc.) and report latency
    after the wire round-trip. The local selection path records the
    same non-secret structured evidence with ``observed_account_id``
    unknown (``None``) -- we never fabricate a positive observed
    account identity when the gateway never ran.
    """
    selection = GatewaySelection(
        requested_account_id=selection_result.selected.account_id,
        provider=selection_result.selected.provider,
        observed_account_id=None,
        selection_reason=selection_result.selection_reason,
        selected_at=timestamp,
        pool_id=selection_result.selected.pool_id,
        free_capacity=selection_result.selected.free_capacity,
        affinity_retained=selection_result.affinity_retained,
        affinity_broken=selection_result.affinity_broken,
        affinity_break_reason=selection_result.affinity_break_reason,
    )
    evidence_payload: dict[str, Any] = {
        "frozen_digest": frozen_digest,
        "gateway_used": True,
        "account_id": selection_result.selected.account_id,
        "provider": selection_result.selected.provider,
        "model": model,
        "pool_id": selection_result.selected.pool_id,
        "free_capacity": selection_result.selected.free_capacity,
        "affinity_retained": selection_result.affinity_retained,
        "affinity_broken": selection_result.affinity_broken,
        "selection_reason": selection_result.selection_reason,
        "selected_at": timestamp,
        "transport_local": True,
    }
    return GatewayDispatchResult(
        success=True,
        kind=GatewayKind.OMNIROUTE,
        requested_provider=provider,
        requested_model=model,
        evidence=evidence_payload,
        target=GatewayDispatchTarget(
            selection=selection,
            health=selection_result.health,
            quota=selection_result.quota,
            request_identifier=f"sel-{timestamp}",
        ),
    )


def prepare_dispatch_decision(
    *,
    base_request: WorkerRequest,
    frozen_gateway: FrozenGatewayConfig | None,
    provider: str,
    model: str,
    transport: GatewayTransport | None = None,
    omniroute_endpoint: str | None = None,
    accounts: tuple[GatewayAccount, ...] = (),
    health_state: GatewayHealthState | None = None,
    affinity_hint: AffinityHint | None = None,
    quota_observations: Iterable[GatewayQuotaObservation] = (),
    pool_observations: Iterable[GatewayQuotaPoolView] = (),
    now: str | None = None,
) -> tuple[WorkerRequest, GatewayDispatchResult]:
    """Prepare a WorkerRequest and gateway evidence for a single dispatch.

    The frozen gateway contract is the authoritative source for whether a
    gateway is consulted at the dispatch boundary. The orchestration
    layer (typically ``Orchestrator.run``) calls this helper *after* C07
    hard policy has admitted the candidate but *before* invoking the
    worker adapter.

    Behaviour:

    * ``frozen_gateway is None`` or ``enabled is False``
      → returns ``base_request`` unchanged and a typed DIRECT evidence
        result. Zero gateway calls, zero selection logic executed.
    * ``enabled is True`` and ``kind == OMNIROUTE``
      → constructs a :class:`GatewayDispatcher` from the frozen config,
        performs *local* account selection (no HTTP transport call),
        augments ``base_request.env`` with the bounded gateway pinning
        env vars (:data:`GATEWAY_ENDPOINT_ENV`, :data:`GATEWAY_CONNECTION_ENV`,
        :data:`GATEWAY_AUTHORIZATION_ENV`, :data:`GATEWAY_FROZEN_DIGEST_ENV`,
        :data:`GATEWAY_SELECTION_REASON_ENV`), and returns a typed
        :class:`GatewayDispatchResult` with the selection evidence.

    This function NEVER makes an HTTP call to the gateway backend.
    The actual model execution is performed by the worker adapter using
    the augmented :class:`WorkerRequest`; the gateway evidence is durably
    associated with that single dispatch via
    :attr:`DispatchEvidence.gateway_evidence_json`.

    When ``omniroute_endpoint`` is missing for an OMNIROUTE-enabled run,
    the helper returns the base request unchanged and a typed UNAVAILABLE
    evidence row; the orchestration layer treats this as infrastructure
    failure (not a model-quality failure) and decides whether to fall
    back to the existing direct worker path or to escalate.
    """
    timestamp = now or utc_now_iso()
    if frozen_gateway is None or not frozen_gateway.enabled:
        return base_request, build_direct_dispatch(provider=provider, model=model)
    if frozen_gateway.kind != GatewayKind.OMNIROUTE:
        raise GatewayManifestError(
            f"unsupported frozen gateway kind: {frozen_gateway.kind.value}"
        )
    if omniroute_endpoint is None:
        return (
            base_request,
            GatewayDispatchResult(
                success=False,
                kind=GatewayKind.OMNIROUTE,
                requested_provider=provider,
                requested_model=model,
                evidence={"frozen_digest": frozen_gateway.digest},
                error=GatewayError(
                    kind=GatewayErrorKind.UNAVAILABLE,
                    message=(
                        "OmniRoute gateway is enabled but no endpoint is "
                        "configured for the dispatcher"
                    ),
                    recoverable=True,
                ),
            ),
        )
    effective_transport = transport if transport is not None else _LocalSelectionTransport()
    dispatcher = GatewayDispatcher.from_frozen_config(
        frozen_config=frozen_gateway,
        accounts=accounts,
        health=health_state,
        omniroute_transport=effective_transport,
        omniroute_endpoint=omniroute_endpoint,
    )
    if dispatcher.omniroute is None:
        return (
            base_request,
            GatewayDispatchResult(
                success=False,
                kind=GatewayKind.OMNIROUTE,
                requested_provider=provider,
                requested_model=model,
                evidence={"frozen_digest": frozen_gateway.digest},
                error=GatewayError(
                    kind=GatewayErrorKind.UNAVAILABLE,
                    message="OmniRoute gateway is not enabled in the dispatcher",
                    recoverable=True,
                ),
            ),
        )
    try:
        selection_result = dispatcher.select_account_for(
            provider=provider,
            model=model,
            affinity_hint=affinity_hint,
            quota_observations=quota_observations,
            pool_observations=pool_observations,
            now=timestamp,
        )
    except GatewayManifestError as exc:
        return (
            base_request,
            GatewayDispatchResult(
                success=False,
                kind=GatewayKind.OMNIROUTE,
                requested_provider=provider,
                requested_model=model,
                evidence={"frozen_digest": frozen_gateway.digest},
                error=GatewayError(
                    kind=GatewayErrorKind.ACCOUNT_UNAVAILABLE,
                    message=str(exc),
                    recoverable=False,
                ),
            ),
        )
    augmented_request = _augment_request_with_gateway(
        base_request,
        endpoint=omniroute_endpoint,
        account_id=selection_result.selected.account_id,
        authorization_env=GATEWAY_AUTHORIZATION_ENV_DEFAULT,
        frozen_digest=frozen_gateway.digest,
        selection_reason=selection_result.selection_reason,
    )
    evidence = _evidence_from_selection(
        selection_result=selection_result,
        frozen_digest=frozen_gateway.digest,
        provider=provider,
        model=model,
        timestamp=timestamp,
    )
    return augmented_request, evidence


def freeze_dispatcher_config(
    *,
    omniroute: OmniRouteBackendConfig | None,
    enabled: bool,
    accounts: Iterable[GatewayAccount] = (),
) -> FrozenGatewayConfig:
    """Freeze a dispatcher config (DIRECT or OMNIROUTE) into a run contract."""
    if omniroute is None:
        return FrozenGatewayConfig(
            schema_version=1,
            kind=GatewayKind.DIRECT,
            enabled=False,
            endpoint=None,
            permitted_provider_models=(),
            direct_fallback_allowed=False,
            affinity={},
            accounts=tuple(accounts),
        )
    frozen = freeze_omniroute_config(omniroute, enabled=enabled, accounts=accounts)
    return frozen


def serialize_dispatch_evidence(result: GatewayDispatchResult) -> str:
    """Return a canonical-JSON serialization of a dispatch result.

    The serialized form is suitable for ``DispatchEvidence.gateway_evidence_json``.
    Secret values are already scrubbed at the contract boundary; this
    helper only performs deterministic JSON encoding.
    """
    return json.dumps(result.as_dict(), sort_keys=True, separators=(",", ":"))


def deserialize_dispatch_evidence(raw: str) -> dict[str, Any]:
    """Inverse of :func:`serialize_dispatch_evidence`."""
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise GatewayManifestError("gateway evidence must be a JSON object")
    return value


def build_direct_evidence(*, provider: str, model: str) -> str:
    """Return the canonical JSON evidence for a DIRECT (no-gateway) dispatch."""
    return serialize_dispatch_evidence(build_direct_dispatch(provider=provider, model=model))


__all__ = [
    "GATEWAY_AUTHORIZATION_ENV",
    "GATEWAY_AUTHORIZATION_ENV_DEFAULT",
    "GATEWAY_CONNECTION_ENV",
    "GATEWAY_ENDPOINT_ENV",
    "GATEWAY_FROZEN_DIGEST_ENV",
    "GATEWAY_LOOPBACK_ENV",
    "GATEWAY_PROVIDER_ENV",
    "GATEWAY_SELECTION_REASON_ENV",
    "GatewayDispatcher",
    "LocalSelectionTransport",
    "build_direct_evidence",
    "deserialize_dispatch_evidence",
    "freeze_dispatcher_config",
    "prepare_dispatch_decision",
    "serialize_dispatch_evidence",
]
