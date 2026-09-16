"""C15: user-owned model access foundation.

This module answers exactly one question:

    HOW can an already-selected :class:`ExecutionBinding` authenticate
    and connect?

It never answers WHICH model Orchestrator should select.  Routing,
provider/model selection, quota eligibility and dispatch eligibility
remain owned by C07 (:mod:`saberops.control_plane.preflight`);
Attempt lifecycle/retries/failover remain owned by C11-D; tool
authorization/sandboxing/receipts remain owned by C08/C11-C; telemetry
remains observational only.  The credential-reference *contract* is
consumed from :mod:`saberops.sandbox.grants` (C08/C11-C remains
the credential/security authority); nothing here re-implements it.

Concept separation (do not collapse these):

* ``ExecutionBinding`` -- WHAT Orchestrator selected
  (:mod:`saberops.control_plane.bindings`).
* :class:`ProviderConnection` -- HOW that selected binding can be
  accessed (this module).
* ``WorkerAdapter`` -- HOW execution is physically performed
  (:mod:`saberops.workers.base`).
* Router / control plane -- WHETHER / WHEN Orchestrator selects the
  binding.

Target flow::

    control_plane
        -> ExecutionBinding
        -> Access Resolver (this module)
        -> ProviderConnection
        -> WorkerAdapter / protocol transport
        -> provider / account / backend

Design conventions applied here are deliberately small and fail-closed:
typed failures, fixed reason vocabularies, secret-free ``repr``\\ s /
durable payloads, and lease/observation separation.  Credential
references use the ``env:`` / ``profile:`` / ``store:`` handle forms
(validated by
:func:`saberops.sandbox.grants.validate_credential_ref`), non-secret
overlay keys, and ``BindingRegistry``-style
``to_dict`` / ``from_mapping`` durability.

Secrets: this module declares no dedicated raw credential-value
field.  Credential-bearing material is represented by
``credential_ref`` handles, and credential-bearing headers persist a
header *name* plus a handle -- never a value.  Literal public headers
are contractually restricted to non-sensitive metadata by a
fail-closed credential-like name rule; like any ordinary string field
they can physically hold arbitrary text, so the guarantee is the
architectural absence of a credential channel, not value scanning.
Rejected input is never echoed back.  Connection ``to_dict`` / ``repr``
output is safe for logs, telemetry and durable evidence by
construction.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "ACCESS_RESOLUTION_REASONS",
    "ACCOUNT_BACKENDS",
    "CONNECTION_TEST_REASONS",
    "AccountBackend",
    "AccessRegistry",
    "AccessResolutionError",
    "AccessStore",
    "AccessStoreError",
    "AuthMode",
    "ConnectionTestReason",
    "ConnectionTestResult",
    "DiscoveryState",
    "ProviderConnection",
    "ResolvedAccess",
    "TriState",
    "AccessProtocol",
    "access_registry_from_mapping",
    "build_access_registry",
    "classify_api_probe",
    "connection_from_mapping",
    "default_account_connections",
    "list_presets",
    "preset_connection",
    "resolve_access",
]


class AccessProtocol(StrEnum):
    """Wire protocol a connection speaks.

    An unknown provider never becomes a new bespoke client: it either
    fits ``OPENAI_COMPATIBLE`` (the generic escape hatch) or it needs
    a new protocol adapter, in which case the protocol stays
    explicitly unsupported until that adapter exists.
    """

    OPENAI_COMPATIBLE = "OPENAI_COMPATIBLE"
    ANTHROPIC = "ANTHROPIC"
    GEMINI = "GEMINI"
    AWS_BEDROCK = "AWS_BEDROCK"
    GOOGLE_VERTEX = "GOOGLE_VERTEX"
    CUSTOM = "CUSTOM"


class AuthMode(StrEnum):
    """How a connection authenticates.

    Account authentication and API-key authentication are distinct
    modes: a subscription is never represented as an API key and an
    API key is never represented as a subscription.
    """

    ACCOUNT_SESSION = "ACCOUNT_SESSION"
    SUBSCRIPTION_BACKEND = "SUBSCRIPTION_BACKEND"
    API_KEY = "API_KEY"
    CREDENTIAL_CHAIN = "CREDENTIAL_CHAIN"
    LOCAL_NO_AUTH = "LOCAL_NO_AUTH"
    CUSTOM = "CUSTOM"


class TriState(StrEnum):
    """Three-valued discovery evidence: TRUE / FALSE / UNKNOWN.

    Capabilities are never inferred from provider branding: anything
    not positively observed stays ``UNKNOWN``.
    """

    TRUE = "TRUE"
    FALSE = "FALSE"
    UNKNOWN = "UNKNOWN"


class ConnectionTestReason(StrEnum):
    """Fixed, typed vocabulary for connection test/probe outcomes."""

    OK = "OK"
    BACKEND_NOT_INSTALLED = "BACKEND_NOT_INSTALLED"
    NOT_AUTHENTICATED = "NOT_AUTHENTICATED"
    CREDENTIAL_MISSING = "CREDENTIAL_MISSING"
    CREDENTIAL_REJECTED = "CREDENTIAL_REJECTED"
    ENDPOINT_UNREACHABLE = "ENDPOINT_UNREACHABLE"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    PROTOCOL_UNSUPPORTED = "PROTOCOL_UNSUPPORTED"
    CAPABILITY_UNKNOWN = "CAPABILITY_UNKNOWN"
    CONFIG_INVALID = "CONFIG_INVALID"


#: Stable reason vocabulary for access-resolution refusals.  Additive only.
ACCESS_RESOLUTION_REASONS: tuple[str, ...] = (
    "NO_REGISTRY",
    "UNKNOWN_CONNECTION",
    "CONNECTION_DISABLED",
    "UNMAPPABLE_PROFILE",
)

#: Every connection-test reason (stable, additive only).
CONNECTION_TEST_REASONS: tuple[str, ...] = tuple(reason.value for reason in ConnectionTestReason)


_HEADER_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]*")

#: Headers owned by the transport layer; neither literal public headers
#: nor the credential-header name may claim them.
_TRANSPORT_RESERVED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
    }
)

#: Bounded deterministic rule for credential-like header names.  The
#: normalized name (lower-cased, separators removed) fails closed when
#: it equals a reserved name or contains any of these fragments.  The
#: guarantee is architectural, not heuristic: secret-bearing headers
#: must use ``auth_header_name`` + ``credential_ref`` (name and handle
#: only, never a value), so literal public headers can only ever carry
#: non-sensitive metadata.
_CREDENTIAL_NAME_FRAGMENTS = frozenset(
    {
        "authorization",
        "proxy",
        "cookie",
        "setcookie",
        "apikey",
        "token",
        "secret",
        "credential",
        "passwd",
        "password",
        "bearer",
        "session",
    }
)


def _header_name_ok(name: object) -> str:
    """Return the stripped header name or fail closed on malformed input.

    The refusal is content-free.  A name that fails the grammar is, by
    definition, arbitrary submitted text rather than header metadata --
    a pasted ``Bearer <secret>`` fragment reaches here as readily as a
    typo -- so echoing it would reflect rejected, possibly
    secret-bearing input back into a rendered error.  A name that
    *passes* the grammar may still be quoted by callers (for example
    the credential-like-name refusal), because it is by construction
    ordinary non-sensitive metadata.
    """
    if not isinstance(name, str) or _HEADER_NAME_RE.fullmatch(name) is None:
        raise ValueError("invalid header name: must match [A-Za-z][A-Za-z0-9-]*")
    return name.strip()


def _validate_public_headers(
    headers: Mapping[str, str],
) -> tuple[tuple[str, str], ...]:
    """Validate literal non-sensitive metadata headers (fail closed).

    Any credential-like name -- authorization, proxy/cookie family, API
    key, token, secret, credential, password or session material -- is
    rejected: such headers must be declared as ``auth_header_name`` +
    ``credential_ref`` instead.  Values are never scanned for secrets;
    the invariant comes from the name rule plus the absence of any
    value-holding credential field.
    """
    if not isinstance(headers, Mapping):
        raise ValueError("public_headers must be a mapping")
    cleaned: list[tuple[str, str]] = []
    for raw_name, value in headers.items():
        name = _header_name_ok(raw_name)
        normalized = name.lower().replace("-", "").replace("_", "")
        if name.lower() in _TRANSPORT_RESERVED_HEADERS or any(
            fragment in normalized for fragment in _CREDENTIAL_NAME_FRAGMENTS
        ):
            raise ValueError(
                f"public_headers must not carry credential-like header: {name!r}; "
                "use auth_header_name + credential_ref instead"
            )
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"public_headers header {name!r} must be non-empty")
        if any(char in "\r\n\x00" for char in value):
            raise ValueError(f"public_headers header {name!r} carries control characters")
        cleaned.append((name, value.strip()))
    return tuple(sorted(cleaned))


def _validate_auth_header_name(name: str, *, credential_ref: str) -> str:
    """Validate a credential-bearing header *name* (never a value).

    Persists only which header the referenced credential binds to at
    the future execution boundary (e.g. ``X-API-Key`` + ``env:KEY``).
    A declared name requires a ``credential_ref`` handle; the value
    itself never exists in this subsystem.
    """
    cleaned = name.strip()
    if not cleaned:
        return ""
    _header_name_ok(cleaned)
    if cleaned.lower() in _TRANSPORT_RESERVED_HEADERS:
        raise ValueError(f"auth_header_name must not claim a transport header: {cleaned!r}")
    if not credential_ref.strip():
        raise ValueError(f"auth_header_name {cleaned!r} requires a credential_ref handle")
    return cleaned


_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def _validate_endpoint(endpoint: str, *, auth_mode: AuthMode) -> str:
    """Validate an endpoint URL without contacting it (fail closed).

    An empty endpoint is allowed at construction: it represents a
    declared-but-not-yet-configured connection (see
    :attr:`ProviderConnection.is_configured`).  Anything that would
    use the connection must refuse an unconfigured one with
    ``CONFIG_INVALID`` instead of attempting network access.
    """
    cleaned = endpoint.strip()
    if not cleaned:
        return ""
    try:
        parts = urlsplit(cleaned)
    except ValueError as exc:
        raise ValueError("endpoint is not a valid URL") from exc
    if parts.scheme not in ("http", "https"):
        raise ValueError("endpoint scheme must be http(s)")
    if not parts.hostname:
        raise ValueError("endpoint has no host")
    if parts.username or parts.password:
        # Embedded userinfo is credential material; it must never appear
        # in a durable connection record.
        raise ValueError("endpoint must not embed credentials (userinfo)")
    if parts.scheme == "http" and parts.hostname not in _LOOPBACK_HOSTS:
        raise ValueError("plain-http endpoints are allowed only for loopback hosts")
    if parts.query:
        raise ValueError("endpoint must not include a query string")
    if parts.fragment:
        raise ValueError("endpoint must not include a fragment")
    if any(char.isspace() or ord(char) < 32 for char in cleaned):
        raise ValueError("endpoint contains whitespace or control characters")
    return cleaned


def _validate_credential_ref(credential_ref: str, *, auth_mode: AuthMode) -> str:
    """Validate the credential handle (never a secret value)."""
    cleaned = credential_ref.strip()
    if not cleaned:
        if auth_mode in (AuthMode.API_KEY, AuthMode.CUSTOM):
            raise ValueError("credential_ref is required for API-key connections")
        return ""
    from saberops.sandbox.grants import validate_credential_ref

    try:
        validate_credential_ref(cleaned)
    except ValueError as exc:
        raise ValueError(f"invalid credential_ref: {exc}") from exc
    return cleaned


@dataclass(frozen=True)
class ProviderConnection:
    """HOW one already-selected binding can be accessed.

    Identity (``connection_id``, ``provider``, ``protocol``,
    ``backend``) is durable; ``credential_ref`` is a handle to the
    secret location (``env:`` / ``profile:`` / ``store:``), never a
    secret value.  Headers are split into two disjoint kinds:

    * ``public_headers`` -- literal non-sensitive metadata only.  Any
      credential-like name fails closed here.
    * ``auth_header_name`` -- the *name* of one credential-bearing
      header (e.g. ``X-API-Key``) bound to ``credential_ref``.  Only
      the name and the handle persist; the secret value never exists
       in this subsystem and is derived at the narrow execution
       boundary.

    There is deliberately no field capable of holding plaintext
    credential material, so ``repr`` / ``to_dict`` output is safe for
    logs, telemetry and durable evidence by construction.

    A connection is not a routing policy: it carries no selection
    preference, no quota policy and no reasoning-effort policy.
    ``account_owner`` identifies the account owner. It is informational in
    the current single-owner configuration.
    """

    connection_id: str
    provider: str
    protocol: AccessProtocol
    backend: str
    auth_mode: AuthMode
    endpoint: str = ""
    credential_ref: str = ""
    public_headers: tuple[tuple[str, str], ...] = ()
    auth_header_name: str = ""
    model: str = ""
    account_owner: str = ""
    enabled: bool = True
    declared_capabilities: frozenset[str] = field(default_factory=frozenset)
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.connection_id.strip():
            raise ValueError("ProviderConnection.connection_id must not be empty")
        object.__setattr__(self, "connection_id", self.connection_id.strip())
        provider = self.provider.strip().lower()
        if not provider:
            raise ValueError(
                f"ProviderConnection '{self.connection_id}': provider must not be empty"
            )
        object.__setattr__(self, "provider", provider)
        backend = self.backend.strip().lower()
        if not backend:
            raise ValueError(
                f"ProviderConnection '{self.connection_id}': backend must not be empty"
            )
        object.__setattr__(self, "backend", backend)
        object.__setattr__(
            self, "endpoint", _validate_endpoint(self.endpoint, auth_mode=self.auth_mode)
        )
        credential_ref = _validate_credential_ref(self.credential_ref, auth_mode=self.auth_mode)
        object.__setattr__(self, "credential_ref", credential_ref)
        object.__setattr__(
            self,
            "public_headers",
            _validate_public_headers(dict(self.public_headers)),
        )
        object.__setattr__(
            self,
            "auth_header_name",
            _validate_auth_header_name(self.auth_header_name, credential_ref=credential_ref),
        )
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "account_owner", self.account_owner.strip())
        caps = frozenset(str(cap).strip().lower() for cap in self.declared_capabilities)
        if any(not cap for cap in caps):
            raise ValueError("declared_capabilities must not contain empty names")
        object.__setattr__(self, "declared_capabilities", caps)
        object.__setattr__(self, "notes", tuple(str(note) for note in self.notes))

    @property
    def is_account_backed(self) -> bool:
        """True when auth is owned by the provider/backend, not by a key."""
        return self.auth_mode in (
            AuthMode.ACCOUNT_SESSION,
            AuthMode.SUBSCRIPTION_BACKEND,
            AuthMode.CREDENTIAL_CHAIN,
        )

    @property
    def is_configured(self) -> bool:
        """True when the connection carries everything its mode needs.

        Account-backed modes are configured by declaration (the backend
        owns authentication).  API modes require an endpoint, and
        ``API_KEY`` / ``CUSTOM`` additionally require a credential
        handle.  Anything unconfigured must fail closed with
        ``CONFIG_INVALID`` at test/resolve time -- never attempt
        network access.
        """
        if self.auth_mode in (
            AuthMode.ACCOUNT_SESSION,
            AuthMode.SUBSCRIPTION_BACKEND,
            AuthMode.CREDENTIAL_CHAIN,
        ):
            return True
        if not self.endpoint:
            return False
        if self.auth_mode in (AuthMode.API_KEY, AuthMode.CUSTOM):
            return bool(self.credential_ref)
        return True

    @property
    def credential_display(self) -> str:
        """Masked credential handle for UI display (never a secret value)."""
        if not self.credential_ref:
            return ""
        scheme = self.credential_ref.partition(":")[0]
        return f"{scheme}:••••••"

    def safe_summary(self) -> dict[str, Any]:
        """Log/telemetry-safe summary (no endpoint query, no headers)."""
        return {
            "connection_id": self.connection_id,
            "provider": self.provider,
            "protocol": self.protocol.value,
            "backend": self.backend,
            "auth_mode": self.auth_mode.value,
            "account_backed": self.is_account_backed,
            "credential_present": bool(self.credential_ref),
            "enabled": self.enabled,
        }

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering (names and handles only, no secret values)."""
        return {
            "connection_id": self.connection_id,
            "provider": self.provider,
            "protocol": self.protocol.value,
            "backend": self.backend,
            "auth_mode": self.auth_mode.value,
            "endpoint": self.endpoint,
            "credential_ref": self.credential_ref,
            "public_headers": [
                {"name": name, "value": value} for name, value in self.public_headers
            ],
            "auth_header_name": self.auth_header_name,
            "model": self.model,
            "account_owner": self.account_owner,
            "enabled": self.enabled,
            "declared_capabilities": sorted(self.declared_capabilities),
            "notes": list(self.notes),
        }

    @property
    def digest(self) -> str:
        """Stable content digest (never depends on wall-clock state)."""
        import hashlib

        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _decode_enabled(payload: Mapping[str, Any], *, connection_id: str) -> bool:
    """Decode the durable ``enabled`` flag strictly (fail closed).

    Only a real JSON/Python boolean is accepted.  Permissive coercion
    is unsafe here: ``bool("false")`` is ``True``, so a malformed
    persisted value could silently *enable* a connection the owner
    disabled.  Strings, integers (``bool`` excepted), ``None`` and
    containers are therefore rejected outright rather than guessed at.

    An absent key keeps the v1 schema default (``enabled=True``), which
    matches :attr:`ProviderConnection.enabled` and the records written
    by :meth:`AccessStore.save`; absence is a legitimate v1 shape, a
    malformed present value is not.
    """
    if "enabled" not in payload:
        return True
    value = payload["enabled"]
    if not isinstance(value, bool):
        raise ValueError(
            f"connection '{connection_id}': enabled must be a boolean, "
            f"got {type(value).__name__}"
        )
    return value


def connection_from_mapping(payload: Mapping[str, Any]) -> ProviderConnection:
    """Reconstruct a connection from its non-secret durable mapping."""
    if not isinstance(payload, Mapping):
        raise ValueError("connection mapping must be a mapping")
    connection_id = str(payload.get("connection_id", "")).strip()
    if not connection_id:
        raise ValueError("connection_id is required")
    try:
        protocol = AccessProtocol(str(payload.get("protocol", "")).strip())
    except ValueError as exc:
        raise ValueError(
            f"connection '{connection_id}': unknown protocol {payload.get('protocol')!r}"
        ) from exc
    try:
        auth_mode = AuthMode(str(payload.get("auth_mode", "")).strip())
    except ValueError as exc:
        raise ValueError(
            f"connection '{connection_id}': unknown auth_mode {payload.get('auth_mode')!r}"
        ) from exc
    raw_public = payload.get("public_headers", ())
    public: dict[str, str] = {}
    if isinstance(raw_public, Mapping):
        public = {str(name): str(value) for name, value in raw_public.items()}
    elif isinstance(raw_public, list):
        for item in raw_public:
            if not isinstance(item, Mapping) or "name" not in item:
                raise ValueError(f"connection '{connection_id}': invalid public_headers entry")
            public[str(item["name"])] = str(item.get("value", ""))
    # Legacy candidate-era shape: accept old ``custom_headers`` only when
    # every name already satisfies the public-header rule; anything
    # credential-like fails closed here instead of persisting.
    raw_legacy = payload.get("custom_headers", ())
    if raw_legacy:
        if raw_public:
            raise ValueError(
                f"connection '{connection_id}': public_headers and "
                "legacy custom_headers must not both be present"
            )
        legacy: dict[str, str] = {}
        if isinstance(raw_legacy, Mapping):
            legacy = {str(name): str(value) for name, value in raw_legacy.items()}
        elif isinstance(raw_legacy, list):
            for item in raw_legacy:
                if not isinstance(item, Mapping) or "name" not in item:
                    raise ValueError(f"connection '{connection_id}': invalid custom_headers entry")
                legacy[str(item["name"])] = str(item.get("value", ""))
        else:
            raise ValueError(f"connection '{connection_id}': invalid custom_headers")
        public = legacy
    enabled = _decode_enabled(payload, connection_id=connection_id)
    try:
        return ProviderConnection(
            connection_id=connection_id,
            provider=str(payload.get("provider", "")),
            protocol=protocol,
            backend=str(payload.get("backend", "")),
            auth_mode=auth_mode,
            endpoint=str(payload.get("endpoint", "")),
            credential_ref=str(payload.get("credential_ref", "")),
            public_headers=tuple(public.items()),
            auth_header_name=str(payload.get("auth_header_name", "")),
            model=str(payload.get("model", "")),
            account_owner=str(payload.get("account_owner", "")),
            enabled=enabled,
            declared_capabilities=frozenset(
                str(cap) for cap in payload.get("declared_capabilities", ())
            ),
            notes=tuple(str(note) for note in payload.get("notes", ())),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid connection mapping: {exc}") from exc


@dataclass(frozen=True)
class DiscoveryState:
    """Truthful, evidence-only discovery for one connection/backend.

    Every signal defaults to ``UNKNOWN``: discovery reports what was
    observed and never infers capabilities from provider branding.
    Discovery is evidence only and never routing authority.
    """

    connection_id: str
    installed: TriState = TriState.UNKNOWN
    authenticated: TriState = TriState.UNKNOWN
    reachable: TriState = TriState.UNKNOWN
    models: tuple[str, ...] = ()
    capabilities: tuple[tuple[str, TriState], ...] = ()

    def capability(self, name: str) -> TriState:
        """Return the observed state of ``name`` (UNKNOWN when unobserved)."""
        for key, state in self.capabilities:
            if key == name.strip().lower():
                return state
        return TriState.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering (non-secret, evidence only)."""
        return {
            "connection_id": self.connection_id,
            "installed": self.installed.value,
            "authenticated": self.authenticated.value,
            "reachable": self.reachable.value,
            "models": list(self.models),
            "capabilities": [
                {"name": name, "state": state.value} for name, state in self.capabilities
            ],
        }


@dataclass(frozen=True)
class ConnectionTestResult:
    """Bounded, classified outcome of one connection probe."""

    connection_id: str
    ok: bool
    reason: ConnectionTestReason
    detail: str = ""
    models: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.ok and self.reason is not ConnectionTestReason.OK:
            raise ValueError("a passing test result must carry reason OK")
        if not self.ok and self.reason is ConnectionTestReason.OK:
            raise ValueError("a failing test result must not carry reason OK")

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering (non-secret detail only)."""
        return {
            "connection_id": self.connection_id,
            "ok": self.ok,
            "reason": self.reason.value,
            "detail": self.detail,
            "models": list(self.models),
        }


def classify_api_probe(
    *,
    credential_configured: bool,
    endpoint_configured: bool = True,
    transport_error: bool = False,
    http_status: int | None = None,
    model_found: bool = True,
    protocol_supported: bool = True,
) -> ConnectionTestReason:
    """Classify an API probe outcome without collapsing it to a boolean.

    Precedence is fail-closed and deterministic: configuration errors
    first, then transport, then authentication, then model identity,
    then protocol support.
    """
    if not endpoint_configured:
        return ConnectionTestReason.CONFIG_INVALID
    if not credential_configured:
        return ConnectionTestReason.CREDENTIAL_MISSING
    if not protocol_supported:
        return ConnectionTestReason.PROTOCOL_UNSUPPORTED
    if transport_error:
        return ConnectionTestReason.ENDPOINT_UNREACHABLE
    if http_status is not None:
        if http_status in (401, 403):
            return ConnectionTestReason.CREDENTIAL_REJECTED
        if http_status == 404:
            return (
                ConnectionTestReason.MODEL_NOT_FOUND
                if model_found is False
                else ConnectionTestReason.ENDPOINT_UNREACHABLE
            )
        if http_status >= 400:
            return ConnectionTestReason.ENDPOINT_UNREACHABLE
    if not model_found:
        return ConnectionTestReason.MODEL_NOT_FOUND
    return ConnectionTestReason.OK


def probe_account_backend(
    connection: ProviderConnection,
    *,
    is_installed: Callable[[], bool] | None = None,
    check_auth: Callable[[], bool | None] | None = None,
    list_models: Callable[[], list[str]] | None = None,
) -> tuple[DiscoveryState, ConnectionTestResult]:
    """Probe an account-backed backend through its supported boundary.

    Orchestrator never extracts account credentials here: ``is_installed``
    answers "executable present", ``check_auth`` answers "supported
    session usable" (``True`` / ``False`` / ``None`` for unknown), and
    ``list_models`` reports exposed models where the backend supports
    it.  All three are injectable so tests use deterministic fakes;
    the default answers ``UNKNOWN`` rather than fabricating evidence.
    """
    if not connection.is_account_backed:
        result = ConnectionTestResult(
            connection_id=connection.connection_id,
            ok=False,
            reason=ConnectionTestReason.CONFIG_INVALID,
            detail="probe_account_backend applies only to account-backed connections",
        )
        return DiscoveryState(connection_id=connection.connection_id), result

    installed = TriState.UNKNOWN
    if is_installed is not None:
        installed = TriState.TRUE if is_installed() else TriState.FALSE
    if installed is TriState.FALSE:
        state = DiscoveryState(connection_id=connection.connection_id, installed=installed)
        return state, ConnectionTestResult(
            connection_id=connection.connection_id,
            ok=False,
            reason=ConnectionTestReason.BACKEND_NOT_INSTALLED,
            detail=f"backend executable for '{connection.backend}' not found",
        )

    authenticated = TriState.UNKNOWN
    if check_auth is not None:
        verdict = check_auth()
        authenticated = (
            TriState.UNKNOWN if verdict is None else (TriState.TRUE if verdict else TriState.FALSE)
        )
    if authenticated is TriState.FALSE:
        state = DiscoveryState(
            connection_id=connection.connection_id,
            installed=installed,
            authenticated=authenticated,
        )
        return state, ConnectionTestResult(
            connection_id=connection.connection_id,
            ok=False,
            reason=ConnectionTestReason.NOT_AUTHENTICATED,
            detail=f"no usable authenticated session for '{connection.backend}'",
        )

    models: tuple[str, ...] = ()
    if list_models is not None:
        try:
            models = tuple(model for model in list_models() if str(model).strip())
        except Exception:
            models = ()
    reachable = TriState.TRUE if authenticated is TriState.TRUE else TriState.UNKNOWN
    state = DiscoveryState(
        connection_id=connection.connection_id,
        installed=installed,
        authenticated=authenticated,
        reachable=reachable,
        models=models,
    )
    if authenticated is TriState.UNKNOWN:
        return state, ConnectionTestResult(
            connection_id=connection.connection_id,
            ok=False,
            reason=ConnectionTestReason.CAPABILITY_UNKNOWN,
            detail="authentication state could not be determined",
            models=models,
        )
    return state, ConnectionTestResult(
        connection_id=connection.connection_id,
        ok=True,
        reason=ConnectionTestReason.OK,
        detail="backend installed and session usable",
        models=models,
    )


@dataclass(frozen=True)
class AccountBackend:
    """Static descriptor binding an account connection to its CLI seam.

    The executable name is used for installed-detection only.
    ``auth_status_argv`` names a documented non-inference status command only
    when its successful result can provide authentication/readiness evidence.
    ``model_list_argv`` names the backend's own *supported*
    model-enumeration subcommand when one exists (e.g. ``codex debug models``);
    an empty tuple means the backend exposes no supported listing seam and
    discovery truthfully stays ``UNKNOWN``.  Empty status/listing tuples mean
    that fact remains unknown. The non-secret argv tails run against the
    backend's own session; Orchestrator never reads session material.
    """

    connection_id: str
    provider: str
    backend: str
    executable: str
    auth_mode: AuthMode = AuthMode.SUBSCRIPTION_BACKEND
    auth_status_argv: tuple[str, ...] = ()
    model_list_argv: tuple[str, ...] = ()


#: The four account-backed connections this foundation represents.
#: Detection reuses each adapter's existing executable; authentication
#: is always owned by the backend itself.
ACCOUNT_BACKENDS: tuple[AccountBackend, ...] = (
    AccountBackend(
        connection_id="codex-chatgpt-account",
        # ``codex`` is the canonical provider identity already used by
        # the authoritative C07/C11-B vocabulary (``ExecutionBinding.
        # provider`` / ``ExecutionProfile.provider`` / capability
        # profiles / control-plane provider policy) and by the existing
        # Codex WorkerAdapter.  It must match exactly: ``resolve_access``
        # is lookup-only, so a divergent identity here would force the
        # caller to rewrite ``codex -> openai``, which this module does
        # not own and deliberately refuses to do.
        provider="codex",
        backend="codex",
        executable="codex",
        auth_mode=AuthMode.ACCOUNT_SESSION,
        auth_status_argv=("doctor", "--json"),
        model_list_argv=("debug", "models"),
    ),
    AccountBackend(
        connection_id="claude-code-subscription",
        provider="anthropic",
        backend="claude-code",
        executable="claude",
        auth_mode=AuthMode.SUBSCRIPTION_BACKEND,
        auth_status_argv=("auth", "status", "--json"),
    ),
    AccountBackend(
        connection_id="copilot-github-account",
        provider="copilot",
        backend="copilot_cli",
        executable="copilot",
        auth_mode=AuthMode.ACCOUNT_SESSION,
    ),
    AccountBackend(
        connection_id="opencode-account",
        provider="opencode",
        backend="opencode",
        executable="opencode",
        auth_mode=AuthMode.ACCOUNT_SESSION,
        model_list_argv=("models",),
    ),
)


def default_account_connections(*, owner: str = "") -> tuple[ProviderConnection, ...]:
    """Return the four account-backed connection declarations.

    Declarations carry no secret bytes: the backend owns
    authentication.  They are distinguishable from API connections by
    (provider, backend, auth_mode): e.g. ``anthropic / claude-code /
    SUBSCRIPTION_BACKEND`` versus ``anthropic / anthropic-api /
    API_KEY``.

    Each declaration's ``provider`` is the canonical provider identity
    of the already-selected binding (``codex``, ``anthropic``,
    ``copilot``, ``opencode``), never a vendor-brand alias:
    :func:`resolve_access` compares providers for exact equality and
    this module owns no normalization, aliasing or routing.
    """
    return tuple(
        ProviderConnection(
            connection_id=descriptor.connection_id,
            provider=descriptor.provider,
            protocol=AccessProtocol.CUSTOM,
            backend=descriptor.backend,
            auth_mode=descriptor.auth_mode,
            account_owner=owner,
            notes=(f"Authenticated {descriptor.executable} session.",),
        )
        for descriptor in ACCOUNT_BACKENDS
    )


_PRESET_DEFAULTS: dict[str, dict[str, Any]] = {
    "minimax": {
        "provider": "minimax",
        "protocol": AccessProtocol.OPENAI_COMPATIBLE,
        "backend": "openai-compatible-api",
        "endpoint": "https://api.minimax.chat/v1",
        "model": "MiniMax-M3",
        "notes": ("MiniMax preset over the generic OpenAI-compatible protocol.",),
    },
    "openrouter": {
        "provider": "openrouter",
        "protocol": AccessProtocol.OPENAI_COMPATIBLE,
        "backend": "openai-compatible-api",
        "endpoint": "https://openrouter.ai/api/v1",
        "model": "",
        "notes": ("OpenRouter preset over the generic OpenAI-compatible protocol.",),
    },
    "general-compute": {
        "provider": "general-compute",
        "protocol": AccessProtocol.OPENAI_COMPATIBLE,
        "backend": "openai-compatible-api",
        "endpoint": "",
        "model": "",
        "notes": ("General Compute preset; endpoint is user-supplied.",),
    },
    "tokenrouter": {
        "provider": "tokenrouter",
        "protocol": AccessProtocol.OPENAI_COMPATIBLE,
        "backend": "openai-compatible-api",
        "endpoint": "",
        "model": "",
        "notes": ("TokenRouter preset; endpoint is user-supplied.",),
    },
    "custom-openai-compatible": {
        "provider": "custom",
        "protocol": AccessProtocol.OPENAI_COMPATIBLE,
        "backend": "openai-compatible-api",
        "endpoint": "",
        "model": "",
        "notes": ("Generic custom OpenAI-compatible endpoint.",),
    },
    "local-openai-compatible": {
        "provider": "local",
        "protocol": AccessProtocol.OPENAI_COMPATIBLE,
        "backend": "openai-compatible-api",
        "endpoint": "http://127.0.0.1:11434/v1",
        "model": "",
        "notes": ("Local OpenAI-compatible server; no credential required.",),
    },
}


def list_presets() -> tuple[str, ...]:
    """Return preset ids (presets prefill; they never route)."""
    return tuple(sorted(_PRESET_DEFAULTS))


def preset_connection(
    preset_id: str,
    *,
    credential_ref: str = "",
    owner: str = "",
    endpoint: str = "",
    model: str = "",
    connection_id: str = "",
) -> ProviderConnection:
    """Build a connection from a named preset over the generic contract.

    Presets only prefill protocol / endpoint / auth convention on the
    shared ``OPENAI_COMPATIBLE`` path; every preset produces an
    ordinary :class:`ProviderConnection` with no independent routing
    behavior.  ``connection_id`` defaults to ``byok-<preset_id>``.
    """
    key = preset_id.strip().lower()
    if key not in _PRESET_DEFAULTS:
        raise ValueError(f"unknown provider preset: {preset_id!r}")
    defaults = _PRESET_DEFAULTS[key]
    resolved_endpoint = endpoint.strip() or str(defaults["endpoint"])
    auth_mode = (
        AuthMode.LOCAL_NO_AUTH
        if key == "local-openai-compatible" and not credential_ref.strip()
        else AuthMode.API_KEY
    )
    return ProviderConnection(
        connection_id=connection_id.strip() or f"byok-{key}",
        provider=str(defaults["provider"]),
        protocol=defaults["protocol"],
        backend=str(defaults["backend"]),
        auth_mode=auth_mode,
        endpoint=resolved_endpoint,
        credential_ref=credential_ref,
        model=model.strip() or str(defaults["model"]),
        account_owner=owner,
        notes=tuple(defaults["notes"]),
    )


class AccessResolutionError(ValueError):
    """Typed refusal to resolve HOW a binding connects (fail closed).

    This is deliberately an ordinary mutable exception, not a frozen
    dataclass: interpreter and context-manager machinery may assign
    exception metadata (e.g. ``__traceback__``), which a frozen
    dataclass would intercept with ``FrozenInstanceError`` and mask
    the intended typed refusal.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ResolvedAccess:
    """The connection HOW for one already-selected binding.

    Resolved from the binding's provider plus the profile's
    non-secret ``backend_ref`` / ``profile_id`` against the
    authoritative :class:`AccessRegistry`.  This value never selects,
    ranks or filters bindings: the caller supplies the selected
    provider identity and receives only the access mechanism for it.
    """

    provider: str
    profile_id: str
    connection: ProviderConnection


@dataclass(frozen=True)
class AccessRegistry:
    """The typed, inspectable registry of user-owned access connections.

    The registry answers ``list_connections`` / ``get`` / ``try_get``
    / ``for_provider``.  No network call participates: it is purely
    deterministic over the configured connections.  Secrets never
    enter: every connection carries a ``credential_ref`` handle at
    most.
    """

    connections: tuple[ProviderConnection, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        ids = [connection.connection_id for connection in self.connections]
        if len(set(ids)) != len(ids):
            raise ValueError("AccessRegistry: duplicate connection_id")

    def list_connections(self) -> tuple[ProviderConnection, ...]:
        """Every configured connection in declaration order."""
        return self.connections

    def get(self, connection_id: str) -> ProviderConnection:
        """Return the connection with this id, or raise :class:`KeyError`."""
        for connection in self.connections:
            if connection.connection_id == connection_id:
                return connection
        raise KeyError(f"AccessRegistry: no connection '{connection_id}'")

    def try_get(self, connection_id: str) -> ProviderConnection | None:
        """Return the connection with this id, or ``None`` if absent."""
        for connection in self.connections:
            if connection.connection_id == connection_id:
                return connection
        return None

    def for_provider(self, provider: str) -> tuple[ProviderConnection, ...]:
        """Connections declaring ``provider`` (account and API kept distinct)."""
        wanted = provider.strip().lower()
        return tuple(connection for connection in self.connections if connection.provider == wanted)

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering (handles only, no secrets)."""
        return {
            "schema_version": self.schema_version,
            "connections": [connection.to_dict() for connection in self.connections],
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> AccessRegistry:
        """Reconstruct a registry from its non-secret durable mapping."""
        if not isinstance(payload, Mapping):
            raise ValueError("access registry must be a mapping")
        schema_version = payload.get("schema_version", 1)
        if schema_version != 1:
            raise ValueError("unsupported access registry schema_version")
        raw = payload.get("connections", [])
        if not isinstance(raw, list) or any(not isinstance(item, Mapping) for item in raw):
            raise ValueError("access registry 'connections' must be a list of mappings")
        try:
            connections = tuple(connection_from_mapping(item) for item in raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid access registry mapping: {exc}") from exc
        return cls(connections=connections, schema_version=int(schema_version))


def build_access_registry(
    connections: tuple[ProviderConnection, ...] | list[ProviderConnection] = (),
) -> AccessRegistry:
    """Construct an :class:`AccessRegistry` over the supplied connections."""
    return AccessRegistry(connections=tuple(connections))


def access_registry_from_mapping(payload: Mapping[str, Any]) -> AccessRegistry:
    """Reconstruct an access registry from serialized non-secret values."""
    return AccessRegistry.from_mapping(payload)


def resolve_access(
    *,
    registry: AccessRegistry | None,
    provider: str,
    profile_id: str = "",
    backend_ref: str = "",
) -> ResolvedAccess:
    """Resolve HOW one already-selected (provider, profile) connects.

    Match order (all non-secret, fail closed):

    1. ``backend_ref`` names a ``connection_id`` directly;
    2. ``profile_id`` names a ``connection_id`` directly;
    3. ``(provider, backend_ref)`` names a ``(provider, backend)`` pair.

    ``provider`` here is the *already-selected* binding provider: this
    function performs a lookup, never a selection.  It raises
    :class:`AccessResolutionError` when no registry exists, nothing
    matches, or the matched connection is disabled.
    """
    if registry is None or not registry.list_connections():
        raise AccessResolutionError("NO_REGISTRY")
    wanted_provider = provider.strip().lower()
    if not wanted_provider:
        raise AccessResolutionError("UNMAPPABLE_PROFILE")
    backend = backend_ref.strip().lower()
    profile = profile_id.strip()
    candidates: list[ProviderConnection] = []
    if backend:
        direct = registry.try_get(backend_ref.strip())
        if direct is not None:
            candidates.append(direct)
    if profile:
        direct = registry.try_get(profile)
        if direct is not None and direct not in candidates:
            candidates.append(direct)
    if backend:
        for connection in registry.for_provider(wanted_provider):
            if connection.backend == backend and connection not in candidates:
                candidates.append(connection)
    for connection in candidates:
        if connection.provider != wanted_provider:
            continue
        if not connection.enabled:
            raise AccessResolutionError("CONNECTION_DISABLED")
        return ResolvedAccess(provider=wanted_provider, profile_id=profile, connection=connection)
    if candidates:
        raise AccessResolutionError("UNMAPPABLE_PROFILE")
    raise AccessResolutionError("UNKNOWN_CONNECTION")


class AccessStoreError(ValueError):
    """Fail-closed error for access-store persistence failures."""


class AccessStore:
    """Durable JSON store for user-added API connections (handles only).

    The store persists :class:`AccessRegistry` mappings -- credential
    *references*, never secret values -- to an explicit path supplied
    by the caller (the web layer wires the owner state dir).  Account
    declarations from :func:`default_account_connections` are always
    layered in front at load time and are never written to disk.
    """

    FILENAME = "access-connections.json"

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        """The file this store reads and writes."""
        return self._path

    def load(self, *, owner: str = "") -> AccessRegistry:
        """Load stored API connections layered behind account declarations."""
        stored: list[ProviderConnection] = []
        if self._path.exists():
            try:
                payload = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise AccessStoreError(f"access store unreadable: {exc}") from exc
            try:
                stored = list(AccessRegistry.from_mapping(payload).list_connections())
            except ValueError as exc:
                raise AccessStoreError(f"access store invalid: {exc}") from exc
        account = [
            connection
            for connection in default_account_connections(owner=owner)
            if connection.connection_id not in {entry.connection_id for entry in stored}
        ]
        return build_access_registry(tuple(account) + tuple(stored))

    def save(self, registry: AccessRegistry) -> None:
        """Persist user-added (non-account) connections; fail closed."""
        account_ids = {descriptor.connection_id for descriptor in ACCOUNT_BACKENDS}
        user_connections = [
            connection.to_dict()
            for connection in registry.list_connections()
            if connection.connection_id not in account_ids
        ]
        payload = {"schema_version": 1, "connections": user_connections}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as exc:
            raise AccessStoreError(f"access store unwritable: {exc}") from exc
