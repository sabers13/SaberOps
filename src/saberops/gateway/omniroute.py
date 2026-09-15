"""OmniRoute provider-neutral gateway backend.

This module implements the only required third-party C10 backend: an
optional :class:`OmniRouteGateway`. It is intentionally small: every
network call goes through an injectable :class:`GatewayTransport`, every
authorization value is supplied at invocation time, and the OmniRoute
``x-omniroute-connection`` pinning header is the only account-level
control surface.

OmniRoute features that would cross the C10 invariant are *never* used:

* No ``auto``, ``combo``, ``fusion``, ``pipeline``, ``context-relay``.
* No MCP tooling (C08 owns tool transport).
* No memory/agent facilities.
* No cross-provider model substitution.

If the gateway cannot serve the *exact* selected provider/model, it
returns a typed infrastructure result. Orch (the workflow layer) is the
only authority that may escalate to another semantic candidate.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from saberops.gateway.accounts import (
    AccountSelectionInputs,
    AccountSelectionResult,
    select_account,
)
from saberops.gateway.affinity import (
    AffinityHint,
    matches_target,
    record_break,
    should_break,
)
from saberops.gateway.client import (
    GatewayHttpRequest,
    GatewayHttpResponse,
    GatewayTransport,
    GatewayTransportError,
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
    freeze_gateway_config,
)
from saberops.gateway.health import (
    GatewayHealthState,
    utc_now_iso,
)
from saberops.gateway.quota import GatewayQuotaPoolView

DEFAULT_TIMEOUT_SECONDS = 60.0
OMNIROUTE_ENV_VAR = "OMNIROUTE_API_KEY"
CHAT_COMPLETIONS_PATH = "/v1/chat/completions"
SELECTED_CONNECTION_HEADER = "x-omniroute-selected-connection-id"
PROVIDER_HEADER = "x-omniroute-provider"
MODEL_HEADER = "x-omniroute-model"
SESSION_HEADER = "x-omniroute-session-id"
DECISION_HEADER = "x-omniroute-decision"
COST_HEADER = "x-omniroute-response-cost"
TOKENS_IN_HEADER = "x-omniroute-tokens-in"
TOKENS_OUT_HEADER = "x-omniroute-tokens-out"
LATENCY_HEADER = "x-omniroute-latency-ms"
CONNECTION_HEADER_NAME = "x-omniroute-connection"


@dataclass(frozen=True)
class OmniRouteDispatchRequest:
    """Inputs to :meth:`OmniRouteGateway.dispatch`."""

    provider: str
    model: str
    messages: tuple[Mapping[str, Any], ...]
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    session_id: str | None = None
    stream: bool = False
    # Required exact provider/model target. The backend never rewrites it.
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise GatewayManifestError("OmniRouteDispatchRequest.provider is required")
        if not self.model.strip():
            raise GatewayManifestError("OmniRouteDispatchRequest.model is required")
        if not self.messages:
            raise GatewayManifestError("OmniRouteDispatchRequest.messages must not be empty")
        if self.timeout_seconds <= 0:
            raise GatewayManifestError("OmniRouteDispatchRequest.timeout_seconds must be positive")


@dataclass(frozen=True)
class OmniRouteBackendConfig:
    """Owner-supplied, non-secret configuration for the OmniRoute backend.

    The configuration is the only surface where the runtime learns about
    OmniRoute; everything else goes through the contracts module.
    """

    endpoint: str
    api_key_env: str = OMNIROUTE_ENV_VAR
    permitted_provider_models: tuple[tuple[str, str], ...] = ()
    direct_fallback_allowed: bool = False
    affinity: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.endpoint.strip():
            raise GatewayManifestError("OmniRoute endpoint must be non-empty")
        if not self.api_key_env.strip():
            raise GatewayManifestError("api_key_env must be non-empty")
        if self.affinity is not None and not isinstance(self.affinity, Mapping):
            raise GatewayManifestError("affinity must be a mapping")

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> OmniRouteBackendConfig:
        endpoint = str(raw.get("endpoint", "")).strip()
        api_key_env = str(raw.get("api_key_env", OMNIROUTE_ENV_VAR)).strip()
        direct_fallback = bool(raw.get("direct_fallback_allowed", False))
        permitted_raw = raw.get("permitted_provider_models", [])
        if not isinstance(permitted_raw, list) or any(
            not isinstance(item, (list, tuple)) or len(item) != 2 for item in permitted_raw
        ):
            raise GatewayManifestError(
                "permitted_provider_models must be a list of [provider, model]"
            )
        affinity = raw.get("affinity", {})
        if not isinstance(affinity, Mapping):
            raise GatewayManifestError("affinity must be an object")
        return cls(
            endpoint=endpoint,
            api_key_env=api_key_env,
            permitted_provider_models=tuple(
                (str(item[0]).strip().lower(), str(item[1]).strip().lower())
                for item in permitted_raw
            ),
            direct_fallback_allowed=direct_fallback,
            affinity=dict(affinity),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "api_key_env": self.api_key_env,
            "permitted_provider_models": [
                [provider, model] for provider, model in sorted(self.permitted_provider_models)
            ],
            "direct_fallback_allowed": self.direct_fallback_allowed,
            "affinity": dict(sorted((self.affinity or {}).items())),
        }


@dataclass
class OmniRouteGateway:
    """The OmniRoute backend.

    Construction accepts an explicit transport; tests substitute a stub
    and never make a real network call. The :attr:`authorization` is
    only ever supplied to :meth:`dispatch_with_authorization` and never
    persisted.
    """

    config: OmniRouteBackendConfig
    accounts: tuple[GatewayAccount, ...]
    transport: GatewayTransport
    health: GatewayHealthState = None  # type: ignore[assignment]
    affinity_hint: AffinityHint | None = None

    def __post_init__(self) -> None:
        if not callable(getattr(self.transport, "post_with_auth", None)):
            raise GatewayManifestError("OmniRouteGateway requires an injectable transport")
        if self.health is None:
            self.health = GatewayHealthState.empty()
        if self.affinity_hint is not None and not isinstance(self.affinity_hint, AffinityHint):
            raise GatewayManifestError("affinity_hint must be AffinityHint or None")

    # --- account selection ---------------------------------------------------

    def _permitted(self, provider: str, model: str) -> bool:
        if not self.config.permitted_provider_models:
            return True
        return (
            provider.strip().lower(),
            model.strip().lower(),
        ) in self.config.permitted_provider_models

    def _matching_accounts(self, provider: str) -> list[GatewayAccount]:
        normalized = provider.strip().lower()
        return [
            account
            for account in self.accounts
            if account.provider.strip().lower() == normalized
        ]

    def select(
        self,
        *,
        provider: str,
        model: str,
        quota_observations: Iterable[GatewayQuotaObservation] = (),
        pool_observations: Iterable[GatewayQuotaPoolView] = (),
        now: str | None = None,
    ) -> AccountSelectionResult:
        """Return the chosen account selection (no dispatch)."""
        if not self._permitted(provider, model):
            raise GatewayManifestError(
                "OmniRoute configuration does not permit the selected provider/model; "
                "this is a configuration defect, not a routing decision"
            )
        accounts = self._matching_accounts(provider)
        timestamp = now or utc_now_iso()
        hint = self.affinity_hint
        # Shared path: affinity break must be durable and pool-aware.
        affinity_account_id: str | None = None
        break_reason: str | None = None
        if hint is not None and matches_target(hint, provider, model):
            health = self.health.get(hint.account_id)
            quota = _matching_quota(
                hint.account_id,
                quota_observations,
                pool_observations,
                accounts=tuple(accounts),
            )
            break_reason = should_break(
                hint,
                accounts=accounts,
                health=health,
                quota=quota,
                red_line_pct=15.0,
            )
            if break_reason is not None:
                record_break(
                    hint,
                    reason=break_reason,
                    new_account_id=None,
                    recorded_at=timestamp,
                )
            elif any(a.account_id == hint.account_id for a in accounts):
                affinity_account_id = hint.account_id
        inputs = AccountSelectionInputs(
            candidate_provider=provider,
            candidate_model=model,
            accounts=tuple(accounts),
            health=self.health,
            quota_observations=tuple(quota_observations),
            pool_observations=tuple(pool_observations),
            affinity_account_id=affinity_account_id,
            now=timestamp,
        )
        result = select_account(inputs)
        if break_reason is not None:
            return AccountSelectionResult(
                selected=result.selected,
                affinity_retained=False,
                affinity_broken=True,
                affinity_break_reason=break_reason,
                selection_reason=result.selection_reason,
                considered=result.considered,
                health=result.health,
                quota=result.quota,
            )
        return result

    # --- dispatch -----------------------------------------------------------

    def dispatch_with_authorization(
        self,
        request: OmniRouteDispatchRequest,
        *,
        authorization: str,
        quota_observations: Iterable[GatewayQuotaObservation] = (),
        pool_observations: Iterable[GatewayQuotaPoolView] = (),
        now: str | None = None,
        affinity_hint: AffinityHint | None = None,
    ) -> GatewayDispatchResult:
        """Select an account, dispatch via the injectable transport, return evidence.

        The :class:`AffinityHint` is captured *only* for evidence; the
        backend never restores an affinity hint from persistence inside a
        dispatch — that is the caller's responsibility.
        """
        effective_hint = affinity_hint if affinity_hint is not None else self.affinity_hint
        timestamp = now or utc_now_iso()
        if not authorization or not authorization.strip():
            return _failed_result(
                request=request,
                kind=GatewayKind.OMNIROUTE,
                error_kind=GatewayErrorKind.AUTH_FAILED,
                message="authorization value is required and must be non-empty",
                evidence={"authorization_present": False},
            )
        try:
            selection_result = self._selection_for_request(
                request, effective_hint, quota_observations, pool_observations, timestamp
            )
        except GatewayManifestError as exc:
            return _failed_result(
                request=request,
                kind=GatewayKind.OMNIROUTE,
                error_kind=GatewayErrorKind.ACCOUNT_UNAVAILABLE,
                message=str(exc),
                evidence={"authorization_present": True},
            )
        account = selection_result.selected
        health = selection_result.health
        quota = selection_result.quota
        target = GatewayDispatchTarget(
            selection=GatewaySelection(
                requested_account_id=account.account_id,
                provider=account.provider,
                observed_account_id=None,
                selection_reason=selection_result.selection_reason,
                selected_at=timestamp,
                pool_id=account.pool_id,
                free_capacity=account.free_capacity,
                affinity_retained=selection_result.affinity_retained,
                affinity_broken=selection_result.affinity_broken,
                affinity_break_reason=selection_result.affinity_break_reason,
            ),
            health=health,
            quota=quota,
            request_identifier=f"req-{uuid.uuid4().hex}",
        )
        http_request = _build_chat_completions_request(
            self.config.endpoint,
            request,
            account,
            target.request_identifier,
        )
        try:
            response = self.transport.post_with_auth(
                http_request, authorization=authorization
            )
        except GatewayTransportError as exc:
            return _failed_result(
                request=request,
                kind=GatewayKind.OMNIROUTE,
                error_kind=GatewayErrorKind.TIMEOUT
                if "timeout" in str(exc).lower()
                else GatewayErrorKind.UNAVAILABLE,
                message=str(exc),
                evidence={
                    "authorization_present": True,
                    "request_identifier": target.request_identifier,
                    "account_id": account.account_id,
                },
                target=target,
            )
        return _classify_response(request=request, response=response, target=target)

    def _selection_for_request(
        self,
        request: OmniRouteDispatchRequest,
        effective_hint: AffinityHint | None,
        quota_observations: Iterable[GatewayQuotaObservation],
        pool_observations: Iterable[GatewayQuotaPoolView],
        now: str,
    ) -> AccountSelectionResult:
        if not self._permitted(request.provider, request.model):
            raise GatewayManifestError(
                "OmniRoute configuration does not permit the selected provider/model"
            )
        accounts = self._matching_accounts(request.provider)
        affinity_account_id: str | None = None
        # Pre-check the affinity hint and break it before selection if it can
        # no longer be honored. This keeps the selection pipeline simple.
        if effective_hint is not None and matches_target(
            effective_hint, request.provider, request.model
        ):
            existing = next(
                (a for a in accounts if a.account_id == effective_hint.account_id),
                None,
            )
            health = self.health.get(effective_hint.account_id)
            quota = _matching_quota(
                effective_hint.account_id,
                quota_observations,
                pool_observations,
                accounts=tuple(accounts),
            )
            break_reason = should_break(
                effective_hint,
                accounts=accounts,
                health=health,
                quota=quota,
                red_line_pct=15.0,
            )
            if break_reason is not None:
                record_break(
                    effective_hint,
                    reason=break_reason,
                    new_account_id=None,
                    recorded_at=now,
                )
                # Suppress the hint for this dispatch; the break was already
                # recorded above. The selection pipeline then proceeds
                # without an affinity hint, but we must durable-mark the break.
                inputs = AccountSelectionInputs(
                    candidate_provider=request.provider,
                    candidate_model=request.model,
                    accounts=tuple(accounts),
                    health=self.health,
                    quota_observations=tuple(quota_observations),
                    pool_observations=tuple(pool_observations),
                    affinity_account_id=None,
                    now=now,
                )
                result = select_account(inputs)
                # Durably mark that the affinity was broken and why, even
                # though we selected a different equivalent account.
                return AccountSelectionResult(
                    selected=result.selected,
                    affinity_retained=False,
                    affinity_broken=True,
                    affinity_break_reason=break_reason,
                    selection_reason=result.selection_reason,
                    considered=result.considered,
                    health=result.health,
                    quota=result.quota,
                )
            elif existing is not None:
                affinity_account_id = effective_hint.account_id
        inputs = AccountSelectionInputs(
            candidate_provider=request.provider,
            candidate_model=request.model,
            accounts=tuple(accounts),
            health=self.health,
            quota_observations=tuple(quota_observations),
            pool_observations=tuple(pool_observations),
            affinity_account_id=affinity_account_id,
            now=now,
        )
        return select_account(inputs)

    def resolve_authorization(self) -> str | None:
        """Return the bearer token, or ``None`` if no secret is configured.

        The caller must never log or persist the returned value. The
        backend itself does not store the secret.
        """
        raw = os.environ.get(self.config.api_key_env)
        if raw is None:
            return None
        value = raw.strip()
        return value or None


def _matching_quota(
    account_id: str,
    quota_observations: Iterable[GatewayQuotaObservation],
    pool_observations: Iterable[GatewayQuotaPoolView],
    *,
    accounts: tuple[GatewayAccount, ...] | None = None,
) -> GatewayQuotaObservation | GatewayQuotaPoolView | None:
    # Shared-pool quota is authoritative: if the account belongs to a pool
    # and a pool observation exists, that pool view governs the break check.
    if accounts is not None:
        for acct in accounts:
            if acct.account_id == account_id and acct.pool_id is not None:
                for view in pool_observations:
                    if view.pool_id == acct.pool_id:
                        return view
                break
    else:
        for view in pool_observations:
            if account_id in getattr(view, "member_account_ids", ()):
                return view
    for observation in quota_observations:
        if observation.account_id == account_id:
            return observation
    return None


def _build_chat_completions_request(
    endpoint: str,
    request: OmniRouteDispatchRequest,
    account: GatewayAccount,
    request_identifier: str,
) -> GatewayHttpRequest:
    # Use the dedicated provider route to lock the dispatch to the
    # specific provider without crossing into OmniRoute Auto/Combo
    # routing. The model id is still required and matches the selected
    # semantic candidate verbatim.
    url = f"{endpoint.rstrip('/')}{CHAT_COMPLETIONS_PATH}"
    headers = {
        "x-omniroute-connection": account.account_id,
        "x-request-id": request_identifier,
    }
    if request.session_id:
        headers[SESSION_HEADER] = request.session_id
    body: dict[str, Any] = {
        "model": request.model,
        "messages": [dict(message) for message in request.messages],
        "stream": bool(request.stream),
    }
    if request.temperature is not None:
        body["temperature"] = float(request.temperature)
    if request.top_p is not None:
        body["top_p"] = float(request.top_p)
    if request.max_tokens is not None:
        body["max_tokens"] = int(request.max_tokens)
    return GatewayHttpRequest(
        method="POST",
        url=url,
        headers=headers,
        body=body,
        timeout_seconds=float(request.timeout_seconds),
    )


def _classify_response(
    *,
    request: OmniRouteDispatchRequest,
    response: GatewayHttpResponse,
    target: GatewayDispatchTarget,
) -> GatewayDispatchResult:
    if response.status_code in {401, 403}:
        return _failed_result(
            request=request,
            kind=GatewayKind.OMNIROUTE,
            error_kind=GatewayErrorKind.AUTH_FAILED,
            message=f"OmniRoute responded with status {response.status_code}",
            evidence={"status_code": response.status_code},
            target=target,
        )
    if response.status_code == 429:
        return _failed_result(
            request=request,
            kind=GatewayKind.OMNIROUTE,
            error_kind=GatewayErrorKind.QUOTA_UNAVAILABLE,
            message="OmniRoute reported quota exhaustion (HTTP 429)",
            evidence={"status_code": response.status_code},
            target=target,
        )
    if response.status_code in {404, 410}:
        return _failed_result(
            request=request,
            kind=GatewayKind.OMNIROUTE,
            error_kind=GatewayErrorKind.ACCOUNT_UNAVAILABLE,
            message=f"OmniRoute reported the account/model as unavailable ({response.status_code})",
            evidence={"status_code": response.status_code},
            target=target,
        )
    if response.status_code >= 500:
        return _failed_result(
            request=request,
            kind=GatewayKind.OMNIROUTE,
            error_kind=GatewayErrorKind.UNAVAILABLE,
            message=f"OmniRoute server error ({response.status_code})",
            evidence={"status_code": response.status_code},
            target=target,
        )
    if response.status_code >= 400:
        return _failed_result(
            request=request,
            kind=GatewayKind.OMNIROUTE,
            error_kind=GatewayErrorKind.PROTOCOL_ERROR,
            message=f"OmniRoute returned an unexpected status ({response.status_code})",
            evidence={"status_code": response.status_code},
            target=target,
        )
    observed = _extract_observed_account(response)
    target_dict = target.as_dict()
    if observed is not None and observed != target.selection.requested_account_id:
        # Positive mismatch: fail closed with typed evidence.
        target_dict["selection"] = {
            **target_dict["selection"],
            "observed_account_id": observed,
        }
        return GatewayDispatchResult(
            success=False,
            kind=GatewayKind.OMNIROUTE,
            requested_provider=request.provider,
            requested_model=request.model,
            evidence={
                "status_code": response.status_code,
                "account_mismatch": True,
                "observed_account_id": observed,
                "requested_account_id": target.selection.requested_account_id,
                "request_identifier": target.request_identifier,
            },
            response_headers=dict(sorted(response.headers.items())),
            target=GatewayDispatchTarget(
                selection=GatewaySelection(
                    requested_account_id=target.selection.requested_account_id,
                    provider=target.selection.provider,
                    observed_account_id=observed,
                    selection_reason=target.selection.selection_reason,
                    selected_at=target.selection.selected_at,
                    pool_id=target.selection.pool_id,
                    free_capacity=target.selection.free_capacity,
                    affinity_retained=target.selection.affinity_retained,
                    affinity_broken=target.selection.affinity_broken,
                    affinity_break_reason=target.selection.affinity_break_reason,
                ),
                health=target.health,
                quota=target.quota,
                request_identifier=target.request_identifier,
            ),
            error=GatewayError(
                kind=GatewayErrorKind.ACCOUNT_MISMATCH,
                message=(
                    "OmniRoute served a different account than the pinned connection; "
                    "failing closed per C10 invariant"
                ),
                recoverable=False,
                request_forwarded=True,
            ),
        )
    usage = _extract_usage(response)
    return GatewayDispatchResult(
        success=True,
        kind=GatewayKind.OMNIROUTE,
        requested_provider=request.provider,
        requested_model=request.model,
        evidence={
            "status_code": response.status_code,
            "request_identifier": target.request_identifier,
            "requested_account_id": target.selection.requested_account_id,
            "latency_ms": response.latency_ms,
            "decision": response.headers.get(DECISION_HEADER),
            "reported_provider": response.headers.get(PROVIDER_HEADER),
            "reported_model": response.headers.get(MODEL_HEADER),
        },
        response_body=dict(response.body),
        response_headers=dict(sorted(response.headers.items())),
        usage=usage,
        latency_ms=response.latency_ms,
        target=GatewayDispatchTarget(
            selection=GatewaySelection(
                requested_account_id=target.selection.requested_account_id,
                provider=target.selection.provider,
                observed_account_id=observed,
                selection_reason=target.selection.selection_reason,
                selected_at=target.selection.selected_at,
                pool_id=target.selection.pool_id,
                free_capacity=target.selection.free_capacity,
                affinity_retained=target.selection.affinity_retained,
                affinity_broken=target.selection.affinity_broken,
                affinity_break_reason=target.selection.affinity_break_reason,
            ),
            health=target.health,
            quota=target.quota,
            request_identifier=target.request_identifier,
        ),
    )


def _extract_observed_account(response: GatewayHttpResponse) -> str | None:
    """Return the positively-reported selected account, or ``None``.

    Current upstream documentation indicates
    ``X-OmniRoute-Selected-Connection-Id`` may be absent on a clean
    first-attempt success even when it appears on fallback/error paths.
    We therefore treat absence as ``unknown`` and never fabricate a
    receipt.
    """
    raw = response.headers.get(SELECTED_CONNECTION_HEADER)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _extract_usage(response: GatewayHttpResponse) -> dict[str, Any] | None:
    usage: dict[str, Any] = {}
    tokens_in = response.headers.get(TOKENS_IN_HEADER)
    if tokens_in is not None:
        try:
            usage["tokens_in"] = int(tokens_in)
        except ValueError:
            return None
    tokens_out = response.headers.get(TOKENS_OUT_HEADER)
    if tokens_out is not None:
        try:
            usage["tokens_out"] = int(tokens_out)
        except ValueError:
            return None
    cost = response.headers.get(COST_HEADER)
    if cost is not None:
        try:
            usage["cost_usd"] = float(cost)
        except ValueError:
            return None
    if not usage:
        return None
    usage["source"] = "omniroute_cost_headers"
    return usage


def _failed_result(
    *,
    request: OmniRouteDispatchRequest,
    kind: GatewayKind,
    error_kind: GatewayErrorKind,
    message: str,
    evidence: dict[str, Any],
    target: GatewayDispatchTarget | None = None,
) -> GatewayDispatchResult:
    return GatewayDispatchResult(
        success=False,
        kind=kind,
        requested_provider=request.provider,
        requested_model=request.model,
        evidence=evidence,
        error=GatewayError(
            kind=error_kind,
            message=message,
            recoverable=error_kind
            in {
                GatewayErrorKind.TIMEOUT,
                GatewayErrorKind.ACCOUNT_UNAVAILABLE,
                GatewayErrorKind.QUOTA_UNAVAILABLE,
            },
            request_forwarded=None,
        ),
        target=target,
    )


def freeze_omniroute_config(
    config: OmniRouteBackendConfig,
    *,
    enabled: bool,
    accounts: Iterable[GatewayAccount] = (),
) -> FrozenGatewayConfig:
    """Return a frozen C10 gateway contract for the OmniRoute backend."""
    return freeze_gateway_config(
        kind=GatewayKind.OMNIROUTE,
        enabled=enabled,
        endpoint=config.endpoint,
        permitted_provider_models=config.permitted_provider_models,
        direct_fallback_allowed=config.direct_fallback_allowed,
        affinity=dict(config.affinity or {}),
        accounts=accounts,
        authorization_env_var=config.api_key_env,
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "OMNIROUTE_ENV_VAR",
    "OmniRouteBackendConfig",
    "OmniRouteDispatchRequest",
    "OmniRouteGateway",
    "freeze_omniroute_config",
]
