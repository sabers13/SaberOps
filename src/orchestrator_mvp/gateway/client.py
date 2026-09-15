"""HTTP transport seam for gateway backends.

Production dispatch never calls network libraries directly: every backend
goes through a small, injectable :class:`GatewayTransport` protocol. The
default :class:`HttpxGatewayTransport` is the only implementation that
talks to a real network; all authoritative tests must substitute a
deterministic stub so the gate stays hermetic.

Authorization is *never* carried in any evidence, frozen configuration,
exception, log line, or telemetry row. The transport receives the
authorization value at invocation time only and never echoes it back.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from orchestrator_mvp.gateway.contracts import GatewayManifestError


@dataclass(frozen=True)
class GatewayHttpRequest:
    """A typed HTTP request to a gateway backend.

    Authorization is deliberately NOT carried on this dataclass. The
    transport receives it separately via
    :meth:`GatewayTransport.post_with_auth` so the authorization value is
    never embedded in any persisted structure.
    """

    method: str
    url: str
    headers: Mapping[str, str]
    body: Mapping[str, Any]
    timeout_seconds: float

    def __post_init__(self) -> None:
        if not self.method.strip():
            raise GatewayManifestError("transport request method must be non-empty")
        if not self.url.strip():
            raise GatewayManifestError("transport request url must be non-empty")
        if self.timeout_seconds <= 0:
            raise GatewayManifestError("transport request timeout_seconds must be positive")
        _reject_secret_header(self.headers)


@dataclass(frozen=True)
class GatewayHttpResponse:
    """A typed HTTP response from a gateway backend.

    ``headers`` are returned as plain lowercase-keyed strings. Body is the
    parsed JSON envelope; transport-layer errors raise a
    :class:`GatewayTransportError` instead of returning a fake response.
    """

    status_code: int
    headers: Mapping[str, str]
    body: Mapping[str, Any]
    latency_ms: float = 0.0

    def __post_init__(self) -> None:
        if not 100 <= self.status_code < 600:
            raise GatewayManifestError("transport response status_code is out of range")
        if self.latency_ms < 0:
            raise GatewayManifestError("transport latency_ms must be non-negative")
        _reject_secret_header(self.headers)


class GatewayTransportError(RuntimeError):
    """Raised when the transport cannot complete a request."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GatewayTransport(Protocol):
    """Injectable HTTP transport seam for gateway backends.

    The transport is the only place where bearer tokens, cookies, or other
    authorization values may exist at runtime. It must never log them,
    echo them in errors, or persist them. Tests substitute a stub.
    """

    def post_with_auth(
        self,
        request: GatewayHttpRequest,
        *,
        authorization: str,
    ) -> GatewayHttpResponse: ...


class HttpxGatewayTransport:
    """The default ``httpx``-backed implementation.

    Tests must replace this with a stub. Authorization is accepted as an
    explicit argument to :meth:`post_with_auth` and is never read from any
    persisted state.
    """

    def __init__(self, *, client: Any | None = None) -> None:
        self._client = client

    def post_with_auth(
        self,
        request: GatewayHttpRequest,
        *,
        authorization: str,
    ) -> GatewayHttpResponse:
        if not authorization or not authorization.strip():
            raise GatewayTransportError("authorization value must be non-empty")
        for key in request.headers:
            if key.lower() == "authorization":
                raise GatewayTransportError(
                    "transport requests must not pre-set the Authorization header"
                )
        payload = json.dumps(request.body, sort_keys=True, separators=(",", ":"))
        merged = dict(request.headers)
        merged["Authorization"] = f"Bearer {authorization.strip()}"
        merged.setdefault("Content-Type", "application/json")
        started = time.monotonic()
        try:
            response = self._perform(request, merged, payload)
        except GatewayTransportError:
            raise
        except Exception as exc:  # pragma: no cover - exercised only in real runs
            raise GatewayTransportError(f"transport failure: {exc}") from exc
        latency_ms = (time.monotonic() - started) * 1000.0
        try:
            decoded = response.json()
        except Exception:
            decoded = {"_raw": getattr(response, "text", "")}
        response_headers = {
            str(key).lower(): str(value)
            for key, value in (getattr(response, "headers", {}) or {}).items()
        }
        return GatewayHttpResponse(
            status_code=int(getattr(response, "status_code", 0)),
            headers=response_headers,
            body=decoded if isinstance(decoded, dict) else {"_raw": decoded},
            latency_ms=latency_ms,
        )

    def _perform(
        self, request: GatewayHttpRequest, headers: Mapping[str, str], payload: str
    ) -> Any:
        if self._client is not None:
            return self._client.post(
                request.url,
                content=payload,
                headers=dict(headers),
                timeout=request.timeout_seconds,
            )
        try:
            import httpx
        except ImportError as exc:
            raise GatewayTransportError(
                "httpx is required for live HttpxGatewayTransport"
            ) from exc
        return httpx.post(
            request.url,
            content=payload,
            headers=dict(headers),
            timeout=request.timeout_seconds,
        )


def _reject_secret_header(headers: Mapping[str, str]) -> None:
    for key in headers:
        lowered = key.lower()
        if lowered in {"authorization", "x-api-key", "cookie", "proxy-authorization"}:
            raise GatewayManifestError(
                f"transport must not carry secret-bearing header: {key}"
            )


__all__ = [
    "GatewayHttpRequest",
    "GatewayHttpResponse",
    "GatewayTransport",
    "GatewayTransportError",
    "HttpxGatewayTransport",
]
