"""Provider-neutral gateway contracts.

C10 introduces an optional, account-level gateway layer that sits beneath
Orch's semantic dispatch. The architecture invariant is non-negotiable:

    Orch selects provider/model -> C07 hard policy -> optional gateway
    -> equivalent account/connection only

This module declares the typed vocabulary used by every C10 backend
(``OMNIROUTE``) and the first-class ``DIRECT`` no-gateway path. A gateway
contract is intentionally minimal: it carries no provider/model authority,
no tier, no role. Selection of an account/connection is a pure
infrastructure decision over an already-fixed semantic candidate.

Secrets never enter contracts, frozen run configuration, evidence rows,
logs, telemetry, or exceptions.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchestrator_mvp.gateway.quota import GatewayQuotaPoolView

_SECRET_KEY_RE = re.compile(
    r"(?:token|secret|password|passwd|api[_-]?key|authorization|bearer|access[_-]?token|refresh[_-]?token)$",
    re.IGNORECASE,
)


class GatewayManifestError(ValueError):
    """Raised when a gateway configuration or evidence value is unsafe."""


class GatewayKind(StrEnum):
    """Identity of the gateway implementation backing a selection.

    ``DIRECT`` is the first-class no-gateway path and is the default. Any
    concrete third-party gateway (e.g. ``OMNIROUTE``) is optional and must
    never change the selected provider/model.
    """

    DIRECT = "DIRECT"
    OMNIROUTE = "OMNIROUTE"


class GatewayStatus(StrEnum):
    """Live infrastructure status of a gateway account/connection."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    COOLDOWN = "COOLDOWN"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class GatewayErrorKind(StrEnum):
    """Typed infrastructure-failure taxonomy returned by a gateway backend.

    These are *infrastructure* outcomes, not semantic tier/capability
    failures. They never trigger automatic SURGEON or tier escalation.
    """

    UNAVAILABLE = "GATEWAY_UNAVAILABLE"
    TIMEOUT = "GATEWAY_TIMEOUT"
    AUTH_FAILED = "GATEWAY_AUTH_FAILED"
    ACCOUNT_UNAVAILABLE = "GATEWAY_ACCOUNT_UNAVAILABLE"
    ACCOUNT_MISMATCH = "GATEWAY_ACCOUNT_MISMATCH"
    QUOTA_UNAVAILABLE = "GATEWAY_QUOTA_UNAVAILABLE"
    PROTOCOL_ERROR = "GATEWAY_PROTOCOL_ERROR"


class GatewayQuotaFreshness(StrEnum):
    """Freshness of a gateway-supplied quota observation, mirrored to C07."""

    FRESH = "FRESH"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class GatewayError:
    """Typed infrastructure outcome from a gateway dispatch attempt."""

    kind: GatewayErrorKind
    message: str
    recoverable: bool = False
    request_forwarded: bool | None = None

    def __post_init__(self) -> None:
        if not str(self.message).strip():
            raise GatewayManifestError("gateway error message must be non-empty")
        if self.request_forwarded is not None and not isinstance(self.request_forwarded, bool):
            raise GatewayManifestError("gateway request_forwarded must be a bool or None")
        # Defensive: errors never carry secret material even if the upstream
        # backend accidentally included it. ``message`` is plain text only.
        _reject_secret_text(self.message)


@dataclass(frozen=True)
class GatewayAccount:
    """Non-secret identity of one gateway account/connection.

    ``account_id`` is the gateway-side identifier used to pin selection
    (``x-omniroute-connection`` for OmniRoute). ``pool_id`` is the C07
    shared-pool reference (None for solo accounts). ``free_capacity`` is a
    pure infrastructure-economic attribute: it never confers semantic
    eligibility, only ranking preference among already-eligible accounts.
    """

    account_id: str
    provider: str
    display_name: str | None = None
    pool_id: str | None = None
    free_capacity: bool = False
    # Non-secret opaque account metadata that may be reconstructed by an
    # equivalent seed. Never contains tokens, keys, or credentials.
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.account_id or not self.account_id.strip():
            raise GatewayManifestError("account_id must be non-empty")
        if not self.provider or not self.provider.strip():
            raise GatewayManifestError("provider must be non-empty")
        if self.pool_id is not None and not str(self.pool_id).strip():
            raise GatewayManifestError("pool_id must be None or non-empty")
        _reject_secrets(self.metadata)

    @property
    def canonical_id(self) -> str:
        """Stable, lower-cased identity used for set/dict membership."""
        return f"{self.provider.lower().strip()}/{self.account_id.strip()}"

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "account_id": self.account_id,
            "provider": self.provider,
            "display_name": self.display_name,
            "pool_id": self.pool_id,
            "free_capacity": self.free_capacity,
        }
        if self.metadata:
            data["metadata"] = dict(sorted(self.metadata.items()))
        return data


@dataclass(frozen=True)
class GatewayHealth:
    """Live infrastructure-health observation for one account."""

    account_id: str
    status: GatewayStatus
    observed_at: str
    cooldown_until: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if not self.account_id.strip():
            raise GatewayManifestError("health account_id must be non-empty")
        if not self.observed_at.strip():
            raise GatewayManifestError("health observed_at must be non-empty")
        if self.cooldown_until is not None and not self.cooldown_until.strip():
            raise GatewayManifestError("health cooldown_until must be a timestamp or None")

    @property
    def is_callable(self) -> bool:
        return self.status in {GatewayStatus.HEALTHY, GatewayStatus.UNKNOWN}

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "status": self.status.value,
            "observed_at": self.observed_at,
            "cooldown_until": self.cooldown_until,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class GatewayQuotaObservation:
    """Normalized gateway quota observation in C07-compatible semantics.

    ``remaining_pct`` is the canonical 0..100 *remaining* percentage that
    C07 already consumes (see :mod:`orchestrator_mvp.control_plane`).
    Unknown observations report ``remaining_pct=None`` and freshness
    ``UNKNOWN``; they never fabricate ``0.0`` or ``100.0``.
    """

    account_id: str
    provider: str
    pool_id: str | None
    remaining_pct: float | None
    observed_at: str
    reset_at: str | None = None
    freshness: GatewayQuotaFreshness = GatewayQuotaFreshness.UNKNOWN
    raw_source: str | None = None

    def __post_init__(self) -> None:
        if not self.account_id.strip():
            raise GatewayManifestError("quota account_id must be non-empty")
        if not self.provider.strip():
            raise GatewayManifestError("quota provider must be non-empty")
        if not self.observed_at.strip():
            raise GatewayManifestError("quota observed_at must be non-empty")
        if self.remaining_pct is not None and not 0.0 <= self.remaining_pct <= 100.0:
            raise GatewayManifestError(
                f"remaining_pct must be between 0 and 100 (got {self.remaining_pct!r})"
            )

    @property
    def is_known(self) -> bool:
        return self.remaining_pct is not None

    def as_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "provider": self.provider,
            "pool_id": self.pool_id,
            "remaining_pct": self.remaining_pct,
            "observed_at": self.observed_at,
            "reset_at": self.reset_at,
            "freshness": self.freshness.value,
            "raw_source": self.raw_source,
        }


@dataclass(frozen=True)
class GatewaySelection:
    """The infrastructure decision: which account/connection serves this dispatch.

    The selection retains a strict distinction between the
    *requested/pinned* account (what Orch asked the gateway to use) and
    the *observed served* account, if the gateway reports one. A positive
    mismatch is a typed infrastructure failure; an absent receipt is
    ``observed=None`` and is not fabricated.
    """

    requested_account_id: str
    provider: str
    observed_account_id: str | None
    selection_reason: str
    selected_at: str
    pool_id: str | None = None
    free_capacity: bool = False
    affinity_retained: bool = False
    affinity_broken: bool = False
    affinity_break_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.requested_account_id.strip():
            raise GatewayManifestError("requested_account_id must be non-empty")
        if not self.provider.strip():
            raise GatewayManifestError("selection provider must be non-empty")
        if not self.selection_reason.strip():
            raise GatewayManifestError("selection_reason must be non-empty")
        if not self.selected_at.strip():
            raise GatewayManifestError("selected_at must be non-empty")
        if self.observed_account_id is not None and not self.observed_account_id.strip():
            raise GatewayManifestError("observed_account_id must be a string or None")
        if self.affinity_retained and self.affinity_broken:
            raise GatewayManifestError("selection cannot both retain and break affinity")

    @property
    def observed_known(self) -> bool:
        return self.observed_account_id is not None

    @property
    def mismatch(self) -> bool:
        return (
            self.observed_account_id is not None
            and self.observed_account_id != self.requested_account_id
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_account_id": self.requested_account_id,
            "provider": self.provider,
            "observed_account_id": self.observed_account_id,
            "selection_reason": self.selection_reason,
            "selected_at": self.selected_at,
            "pool_id": self.pool_id,
            "free_capacity": self.free_capacity,
            "affinity_retained": self.affinity_retained,
            "affinity_broken": self.affinity_broken,
            "affinity_break_reason": self.affinity_break_reason,
        }


@dataclass(frozen=True)
class GatewayDispatchTarget:
    """The complete infrastructure target chosen for one dispatch attempt."""

    selection: GatewaySelection
    health: GatewayHealth | None
    quota: GatewayQuotaObservation | GatewayQuotaPoolView | None
    request_identifier: str

    def __post_init__(self) -> None:
        if not self.request_identifier.strip():
            raise GatewayManifestError("request_identifier must be non-empty")
        if (
            self.health is not None
            and self.health.account_id != self.selection.requested_account_id
        ):
            raise GatewayManifestError(
                "health account_id must match selection.requested_account_id"
            )
        if self.quota is not None and hasattr(self.quota, "account_id"):
            if self.quota.account_id != self.selection.requested_account_id:
                raise GatewayManifestError(
                    "quota account_id must match selection.requested_account_id"
                )

    def as_dict(self) -> dict[str, Any]:
        return {
            "selection": self.selection.as_dict(),
            "health": self.health.as_dict() if self.health is not None else None,
            "quota": self.quota.as_dict() if self.quota is not None else None,
            "request_identifier": self.request_identifier,
        }


@dataclass(frozen=True)
class GatewayDispatchResult:
    """Outcome of one gateway dispatch attempt.

    On success, ``response_body`` is the OpenAI-compatible JSON envelope
    and ``evidence`` captures the non-secret observations. On typed
    failure, ``error`` carries the failure classification and ``evidence``
    still captures what was observed before the failure.
    """

    success: bool
    kind: GatewayKind
    requested_provider: str
    requested_model: str
    evidence: dict[str, Any]
    response_body: dict[str, Any] | None = None
    response_headers: dict[str, str] | None = None
    usage: dict[str, Any] | None = None
    latency_ms: float | None = None
    error: GatewayError | None = None
    target: GatewayDispatchTarget | None = None

    def __post_init__(self) -> None:
        if self.success and self.error is not None:
            raise GatewayManifestError("successful results cannot carry an error")
        if not self.success and self.error is None:
            raise GatewayManifestError("failed results must carry a typed error")
        _reject_secrets(self.evidence)
        _reject_secrets(self.usage)
        _reject_secret_text(json.dumps(self.response_headers or {}, default=str))
        _reject_secret_text(json.dumps(self.response_body or {}, default=str))

    def as_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "kind": self.kind.value,
            "requested_provider": self.requested_provider,
            "requested_model": self.requested_model,
            "evidence": dict(sorted(self.evidence.items())),
            "response_body": self.response_body,
            "response_headers": self.response_headers,
            "usage": self.usage,
            "latency_ms": self.latency_ms,
            "error": (
                {
                    "kind": self.error.kind.value,
                    "message": self.error.message,
                    "recoverable": self.error.recoverable,
                    "request_forwarded": self.error.request_forwarded,
                }
                if self.error is not None
                else None
            ),
            "target": self.target.as_dict() if self.target is not None else None,
        }


_DEFAULT_AUTHORIZATION_ENV_VAR = "OMNIROUTE_API_KEY"


@dataclass(frozen=True)
class FrozenGatewayConfig:
    """Immutable, non-secret C10 gateway contract captured for one run.

    The contract freezes the run's gateway kind, endpoint identity,
    permitted provider/model mapping, direct-fallback permission, and
    bounded account/connection metadata needed to reconstruct the run.
    Live infrastructure observations (health, quota) are NOT frozen here.
    """

    schema_version: int
    kind: GatewayKind
    enabled: bool
    endpoint: str | None
    permitted_provider_models: tuple[tuple[str, str], ...]
    direct_fallback_allowed: bool
    affinity: dict[str, Any]
    accounts: tuple[GatewayAccount, ...]
    digest: str = ""
    # Non-secret env-var reference for the gateway credential (e.g. OMNIROUTE_API_KEY).
    # The VALUE never enters the frozen contract. Backward-compatible: old payloads
    # without this field reconstruct as the default.
    authorization_env_var: str = _DEFAULT_AUTHORIZATION_ENV_VAR

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise GatewayManifestError("unsupported gateway schema_version")
        if self.kind == GatewayKind.DIRECT and self.enabled:
            raise GatewayManifestError("DIRECT gateway cannot be both enabled and a no-op")
        for pair in self.permitted_provider_models:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise GatewayManifestError(
                    "permitted_provider_models must be (provider, model) tuples"
                )
        _reject_secrets(self.affinity)
        # Accounts here are infrastructure metadata only; they are frozen to
        # preserve reconstructability but never carry credentials.
        for account in self.accounts:
            _reject_secrets(account.metadata)
        if not self.authorization_env_var or not self.authorization_env_var.strip():
            raise GatewayManifestError("authorization_env_var must be non-empty")
        # The env-var name itself must not look like a secret value, and the
        # contract never carries the secret value.
        if " " in self.authorization_env_var or "=" in self.authorization_env_var:
            raise GatewayManifestError("authorization_env_var must be a plain env-var name")
        _reject_secret_text(self.authorization_env_var)

    def payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "kind": self.kind.value,
            "enabled": self.enabled,
            "endpoint": self.endpoint,
            "permitted_provider_models": [
                [provider, model] for provider, model in sorted(self.permitted_provider_models)
            ],
            "direct_fallback_allowed": self.direct_fallback_allowed,
            "affinity": dict(sorted(self.affinity.items())),
            "accounts": [account.as_dict() for account in sorted(self.accounts, key=_account_sort)],
        }
        # Backward compatibility: only include authorization_env_var when it
        # diverges from the default. Old payloads without the field continue
        # to hash to the same digest when they used the default.
        if self.authorization_env_var != _DEFAULT_AUTHORIZATION_ENV_VAR:
            payload["authorization_env_var"] = self.authorization_env_var
        return payload

    def as_dict(self) -> dict[str, Any]:
        data = self.payload()
        data["digest"] = self.digest or _digest(self.payload())
        return data

    def as_json(self) -> str:
        return _canonical_json(self.as_dict())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> FrozenGatewayConfig:
        if not isinstance(raw, dict):
            raise GatewayManifestError("frozen gateway config must be a JSON object")
        if raw.get("schema_version") != 1:
            raise GatewayManifestError("unsupported frozen gateway schema_version")
        kind_raw = str(raw.get("kind", GatewayKind.DIRECT.value))
        try:
            kind = GatewayKind(kind_raw)
        except ValueError as exc:
            raise GatewayManifestError(f"unknown gateway kind: {kind_raw}") from exc
        permitted_raw = raw.get("permitted_provider_models", [])
        if not isinstance(permitted_raw, list) or any(
            not isinstance(item, (list, tuple)) or len(item) != 2 for item in permitted_raw
        ):
            raise GatewayManifestError(
                "permitted_provider_models must be a list of [provider, model]"
            )
        affinity_raw = raw.get("affinity", {})
        if not isinstance(affinity_raw, dict):
            raise GatewayManifestError("affinity must be a JSON object")
        accounts_raw = raw.get("accounts", [])
        if not isinstance(accounts_raw, list):
            raise GatewayManifestError("accounts must be a list")
        accounts = tuple(_account_from_dict(item) for item in accounts_raw)
        auth_env_raw = raw.get("authorization_env_var", _DEFAULT_AUTHORIZATION_ENV_VAR)
        # Normalize: missing or empty -> default, but validate via __post_init__
        if not isinstance(auth_env_raw, str) or not auth_env_raw.strip():
            auth_env_raw = _DEFAULT_AUTHORIZATION_ENV_VAR
        auth_env_raw = auth_env_raw.strip()
        config = cls(
            schema_version=1,
            kind=kind,
            enabled=bool(raw.get("enabled", False)),
            endpoint=str(raw["endpoint"]) if raw.get("endpoint") else None,
            permitted_provider_models=tuple(
                (str(item[0]), str(item[1])) for item in permitted_raw
            ),
            direct_fallback_allowed=bool(raw.get("direct_fallback_allowed", False)),
            affinity=dict(affinity_raw),
            accounts=accounts,
            digest=str(raw.get("digest", "")),
            authorization_env_var=auth_env_raw,
        )
        expected = _digest(config.payload())
        if not config.digest:
            object.__setattr__(config, "digest", expected)
        elif config.digest != expected:
            # Backward compat: old snapshots without the field hashed without it.
            # If this snapshot lacked the field and our payload now includes it
            # (only when non-default), the above check already handles default
            # case. But if raw had no field and we are default, expected matches
            # old. If raw had field with default value explicitly, tolerate by
            # recomputing without field.
            if (
                "authorization_env_var" not in raw
                and auth_env_raw == _DEFAULT_AUTHORIZATION_ENV_VAR
            ):
                # Old snapshot: digest computed without field — already correct,
                # so mismatch should not happen here. Keep error.
                raise GatewayManifestError("frozen gateway digest mismatch")
            # For payloads that explicitly stored default, try alternative digest
            # without field to remain compatible with historical default digests.
            alt_payload = dict(config.payload())
            alt_payload.pop("authorization_env_var", None)
            alt_expected = _digest(alt_payload)
            if config.digest != alt_expected:
                raise GatewayManifestError("frozen gateway digest mismatch")
        return config


def freeze_gateway_config(
    *,
    kind: GatewayKind,
    enabled: bool,
    endpoint: str | None,
    permitted_provider_models: Iterable[tuple[str, str]] = (),
    direct_fallback_allowed: bool = False,
    affinity: Mapping[str, Any] | None = None,
    accounts: Iterable[GatewayAccount] = (),
    authorization_env_var: str = _DEFAULT_AUTHORIZATION_ENV_VAR,
) -> FrozenGatewayConfig:
    """Capture the run's gateway configuration as an immutable contract."""
    normalized = tuple(
        sorted(
            {
                (provider.strip().lower(), model.strip().lower())
                for provider, model in permitted_provider_models
            }
        )
    )
    affinity_dict = dict(affinity or {})
    _reject_secrets(affinity_dict)
    accounts_tuple = tuple(accounts)
    auth_env = (
        authorization_env_var.strip()
        if isinstance(authorization_env_var, str)
        else _DEFAULT_AUTHORIZATION_ENV_VAR
    )
    if not auth_env:
        auth_env = _DEFAULT_AUTHORIZATION_ENV_VAR
    frozen = FrozenGatewayConfig(
        schema_version=1,
        kind=kind,
        enabled=enabled,
        endpoint=endpoint,
        permitted_provider_models=normalized,
        direct_fallback_allowed=direct_fallback_allowed,
        affinity=affinity_dict,
        accounts=accounts_tuple,
        digest="",
        authorization_env_var=auth_env,
    )
    object.__setattr__(frozen, "digest", _digest(frozen.payload()))
    return frozen


_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _account_sort(account: GatewayAccount) -> str:
    return account.canonical_id


def _account_from_dict(raw: object) -> GatewayAccount:
    if not isinstance(raw, dict):
        raise GatewayManifestError("gateway account entry must be an object")
    return GatewayAccount(
        account_id=str(raw.get("account_id", "")),
        provider=str(raw.get("provider", "")),
        display_name=str(raw["display_name"]) if raw.get("display_name") else None,
        pool_id=str(raw["pool_id"]) if raw.get("pool_id") else None,
        free_capacity=bool(raw.get("free_capacity", False)),
        metadata=dict(raw.get("metadata", {})) if isinstance(raw.get("metadata"), dict) else {},
    )


def _reject_secrets(value: object, key: str = "") -> None:
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            key_text = str(child_key)
            if _SECRET_KEY_RE.search(key_text):
                raise GatewayManifestError(
                    f"secret-bearing gateway field is not permitted: {key_text}"
                )
            _reject_secrets(child_value, key_text)
    elif isinstance(value, list):
        for child in value:
            _reject_secrets(child, key)
    elif isinstance(value, tuple):
        for child in value:
            _reject_secrets(child, key)


def _reject_secret_text(value: str) -> None:
    lowered = value.lower()
    if (
        "bearer " in lowered
        or "-----begin " in lowered
        or re.search(r"(?:token|api[_-]?key|password)=", lowered) is not None
    ):
        raise GatewayManifestError("obvious secret material is not permitted in a gateway value")


__all__ = [
    "GatewayAccount",
    "GatewayDispatchResult",
    "GatewayDispatchTarget",
    "GatewayError",
    "GatewayErrorKind",
    "GatewayHealth",
    "GatewayKind",
    "GatewayManifestError",
    "GatewayQuotaFreshness",
    "GatewayQuotaObservation",
    "GatewaySelection",
    "GatewayStatus",
    "FrozenGatewayConfig",
    "freeze_gateway_config",
    "reconstruct_frozen_gateway_config",
]


def reconstruct_frozen_gateway_config(
    snapshot: Mapping[str, Any],
    *,
    expected_digest: str | None = None,
) -> FrozenGatewayConfig:
    """Reconstruct and integrity-check a persisted C10 gateway contract.

    The :func:`freeze_gateway_config` shape is JSON-friendly; the runtime
    can therefore persist it as a JSON object and restore it during
    continuation. This helper validates the schema, recomputes the
    canonical digest, and returns the canonical contract.
    """
    payload = dict(snapshot)
    config = FrozenGatewayConfig.from_dict(payload)
    if expected_digest is not None and config.digest != str(expected_digest):
        raise GatewayManifestError("frozen gateway snapshot digest mismatch")
    return config
