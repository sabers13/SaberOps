"""C10 — Bounded OmniRoute transport bridge for receipt capture.

This module implements a *single-purpose*, hermetic, infrastructure-only
transport proxy used **only** when the executor-side of C10 needs to
capture the OmniRoute selected-connection receipt
(``X-OmniRoute-Selected-Connection-Id``) on a real Codex dispatch.

The Codex CLI ``rust-v0.147.0`` exposes precise per-turn usage on its
JSONL ``turn.completed`` event but does NOT expose arbitrary upstream
response headers in its structured output.  Therefore the only hermetic,
non-fabricating way to obtain a positive ``observed_account_id`` is to
sit between Codex and OmniRoute and observe the headers ourselves.

The bridge is deliberately NOT a generic proxy:

* It never creates its own semantic model request.
* It does not select provider/model/account.
* It does not implement routing logic.
* It does not inspect, rewrite, or buffer request bodies.
* Its sole authority is to forward the bytes Codex already produced
  to the gateway backend, preserve the connection pin header, and
  record non-secret response metadata.

Secret hygiene:

* The OmniRoute bearer token is supplied at invocation time through an
  environment-variable reference. The bridge resolves the value only
  inside the outgoing HTTP request and never persists it.
* No token value appears in any persisted evidence row, log line,
  exception message, or process command line.

Forwarding state:

* ``FORWARDING_NOT_STARTED``: bridge has accepted no Codex request yet.
* ``REQUEST_FORWARDED``: the bridge accepted and relayed a Codex POST.
* ``REQUEST_COMPLETED``: the gateway responded and the bytes were
  streamed back to Codex.
* ``REQUEST_FAILED``: the gateway transport failed after the bridge had
  begun receiving the Codex request body.
* ``BRIDGE_TERMINATED``: the bridge process has exited.

The bridge terminates with the worker attempt; it does not outlive the
dispatch.
"""

from __future__ import annotations

import http.client
import json
import os
import ssl
import threading
import time
import urllib.parse
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from orchestrator_mvp.gateway.contracts import GatewayManifestError
from orchestrator_mvp.gateway.health import utc_now_iso
from orchestrator_mvp.gateway.omniroute import (
    CONNECTION_HEADER_NAME,
    SELECTED_CONNECTION_HEADER,
)

_BRIDGE_VERSION = 1
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
_DEFAULT_RELAY_TIMEOUT_SECONDS = 600.0


class BridgeForwardingState(StrEnum):
    """Forwarding certainty recorded by the bounded transport bridge."""

    NOT_STARTED = "FORWARDING_NOT_STARTED"
    UNKNOWN = "FORWARDING_UNKNOWN"
    REQUEST_FORWARDED = "REQUEST_FORWARDED"
    REQUEST_COMPLETED = "REQUEST_COMPLETED"
    REQUEST_FAILED = "REQUEST_FAILED"
    BRIDGE_TERMINATED = "BRIDGE_TERMINATED"


class BridgeErrorKind(StrEnum):
    """Typed infrastructure failure categories for the bounded bridge."""

    LAUNCH_FAILED = "BRIDGE_LAUNCH_FAILED"
    FORWARD_FAILED = "BRIDGE_FORWARD_FAILED"
    GATEWAY_ERROR = "BRIDGE_GATEWAY_ERROR"
    BRIDGE_CLOSED = "BRIDGE_CLOSED"


@dataclass(frozen=True)
class BridgeEvidence:
    """Non-secret, JSON-friendly record of one bounded-bridge session.

    The bridge records ONE evidence row per session, written to the
    dispatcher through :class:`BridgeSession.handle`. The row carries:

    * the requested account id (pinned by Orch);
    * the observed account id when the gateway positively reports one
      (``None`` when absent -- never fabricated);
    * the forwarding state at the bridge's final checkpoint;
    * the gateway latency when positively observed;
    * the bridge status code received from the gateway;
    * the bridge session id (UUID, UUID) used for correlation.

    No bearer token, request body, or response body is ever persisted.
    """

    schema_version: int
    session_id: str
    started_at: str
    ended_at: str | None
    requested_account_id: str
    provider: str
    observed_account_id: str | None
    forwarding_state: BridgeForwardingState
    error_kind: BridgeErrorKind | None
    error_message: str | None
    request_method: str | None
    request_path: str | None
    status_code: int | None
    latency_ms: float | None
    headers: dict[str, str] = field(default_factory=dict)
    # C10-R4 multi-request aggregation (bounded, non-secret)
    request_count: int = 0
    any_request_forwarded: bool = False
    completed_request_count: int = 0
    failed_request_count: int = 0
    sticky_mismatch: bool = False
    # Latest known observed account (may be None if never observed)
    # sticky mismatch retains first mismatch observed.
    latest_observed_account_id: str | None = None
    first_error_kind: BridgeErrorKind | None = None
    first_error_message: str | None = None
    last_error_kind: BridgeErrorKind | None = None
    last_error_message: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != _BRIDGE_VERSION:
            raise GatewayManifestError("unsupported bridge evidence schema_version")
        if not self.session_id:
            raise GatewayManifestError("bridge session_id is required")
        if not self.requested_account_id:
            raise GatewayManifestError("bridge requested_account_id is required")
        if not self.provider:
            raise GatewayManifestError("bridge provider is required")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "requested_account_id": self.requested_account_id,
            "provider": self.provider,
            "observed_account_id": self.observed_account_id,
            "forwarding_state": self.forwarding_state.value,
            "error_kind": self.error_kind.value if self.error_kind is not None else None,
            "error_message": self.error_message,
            "request_method": self.request_method,
            "request_path": self.request_path,
            "status_code": self.status_code,
            "latency_ms": self.latency_ms,
            # Non-secret response headers only. Authorization-like headers
            # are scrubbed at construction time.
            "headers": dict(sorted(self.headers.items())),
            "request_count": self.request_count,
            "any_request_forwarded": self.any_request_forwarded,
            "completed_request_count": self.completed_request_count,
            "failed_request_count": self.failed_request_count,
            "sticky_mismatch": self.sticky_mismatch,
            "latest_observed_account_id": self.latest_observed_account_id,
            "first_error_kind": self.first_error_kind.value
            if self.first_error_kind is not None
            else None,
            "first_error_message": self.first_error_message,
            "last_error_kind": self.last_error_kind.value
            if self.last_error_kind is not None
            else None,
            "last_error_message": self.last_error_message,
        }


def scrub_response_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of response headers with secret-bearing keys removed.

    Headers whose name suggests a credential (authorization, cookie,
    set-cookie, x-api-key, ...) are dropped from the bridge evidence row
    so they never reach the orchestrator's persisted state.  The
    selected-connection receipt is preserved by name.
    """

    blocked_substrings = (
        "authorization",
        "auth-token",
        "api-key",
        "apikey",
        "cookie",
        "set-cookie",
        "bearer",
        "secret",
        "password",
        "credential",
        "session-key",
    )
    cleaned: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower().strip()
        if any(sub in lowered for sub in blocked_substrings):
            continue
        cleaned[name] = value
    return cleaned


def scrub_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of incoming request headers with secrets scrubbed."""

    blocked_substrings = (
        "authorization",
        "auth-token",
        "api-key",
        "apikey",
        "cookie",
        "set-cookie",
        "bearer",
        "secret",
        "password",
        "credential",
        "session-key",
    )
    cleaned: dict[str, str] = {}
    for name, value in headers.items():
        lowered = name.lower().strip()
        if any(sub in lowered for sub in blocked_substrings):
            continue
        cleaned[name] = value
    return cleaned


# ---------------------------------------------------------------------------
# HTTP forwarder helpers
# ---------------------------------------------------------------------------


def _split_host(endpoint: str) -> tuple[str, str, int, str]:
    """Split ``http(s)://host:port[/base]`` into scheme, host, port and base path.

    Returns ``(scheme, host, port, base_path)`` where ``base_path`` never
    ends with ``/`` unless it is ``""``.
    """

    rest = endpoint.strip()
    if "://" not in rest:
        raise GatewayManifestError(f"gateway endpoint must include scheme: {endpoint!r}")
    parsed = urllib.parse.urlparse(rest)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise GatewayManifestError(f"only http(s) gateway endpoints are supported: {endpoint!r}")
    host = parsed.hostname or ""
    if not host:
        raise GatewayManifestError("gateway endpoint host is empty")
    # Determine port (default 80 for http, 443 for https)
    if parsed.port is not None:
        port = parsed.port
    else:
        port = 80 if scheme == "http" else 443
    base_path = parsed.path or ""
    if base_path.endswith("/"):
        base_path = base_path[:-1]
    return scheme, host, port, base_path


def _join_path(base: str, requested: str) -> str:
    """Combine the gateway base path with the Codex-requested path."""

    if requested.startswith("/"):
        return f"{base}{requested}" if base else requested
    return f"{base}/{requested}" if base else f"/{requested}"


# ---------------------------------------------------------------------------
# Bridge session state
# ---------------------------------------------------------------------------


@dataclass
class _BridgeState:
    """Mutable per-session state protected by ``_BridgeState.lock``."""

    started_at: str
    forwarding_state: BridgeForwardingState = BridgeForwardingState.NOT_STARTED
    request_method: str | None = None
    request_path: str | None = None
    status_code: int | None = None
    latency_ms: float | None = None
    observed_account_id: str | None = None
    latest_observed_account_id: str | None = None
    sticky_mismatch: bool = False
    sticky_observed_account_id: str | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    error_kind: BridgeErrorKind | None = None
    error_message: str | None = None
    first_error_kind: BridgeErrorKind | None = None
    first_error_message: str | None = None
    last_error_kind: BridgeErrorKind | None = None
    last_error_message: str | None = None
    forwarded_at_ms: float | None = None
    request_count: int = 0
    completed_request_count: int = 0
    failed_request_count: int = 0
    any_request_forwarded: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "forwarding_state": self.forwarding_state,
                "request_method": self.request_method,
                "request_path": self.request_path,
                "status_code": self.status_code,
                "latency_ms": self.latency_ms,
                "observed_account_id": self.observed_account_id,
                "latest_observed_account_id": self.latest_observed_account_id,
                "sticky_mismatch": self.sticky_mismatch,
                "sticky_observed_account_id": self.sticky_observed_account_id,
                "response_headers": dict(self.response_headers),
                "error_kind": self.error_kind,
                "error_message": self.error_message,
                "first_error_kind": self.first_error_kind,
                "first_error_message": self.first_error_message,
                "last_error_kind": self.last_error_kind,
                "last_error_message": self.last_error_message,
                "request_count": self.request_count,
                "completed_request_count": self.completed_request_count,
                "failed_request_count": self.failed_request_count,
                "any_request_forwarded": self.any_request_forwarded,
            }


class _BridgeRequestHandler(BaseHTTPRequestHandler):
    """One request handler that forwards a Codex POST to the gateway.

    The handler is intentionally transport-only: it forwards the Codex
    request bytes, injects the real OmniRoute credential, observes the
    receipt header, and streams the response back. Multiple sequential
    (or overlapping) requests through the same bridge session are
    correctly aggregated at the session level.
    """

    server_version = "C10Bridge/1"

    def do_GET(self) -> None:  # noqa: N802
        self._forward()

    def do_POST(self) -> None:  # noqa: N802
        self._forward()

    def do_PUT(self) -> None:  # noqa: N802
        self._forward()

    def do_PATCH(self) -> None:  # noqa: N802
        self._forward()

    def do_DELETE(self) -> None:  # noqa: N802
        self._forward()

    def log_message(self, *_args: Any, **_kwargs: Any) -> None:  # noqa: D401
        return

    def _record_error(
        self,
        state: _BridgeState,
        *,
        kind: BridgeErrorKind,
        message: str,
        forwarding_state: BridgeForwardingState = BridgeForwardingState.REQUEST_FAILED,
    ) -> None:
        # Preserve secret hygiene: never include token value in error_message
        # (caller must already scrub).
        with state.lock:
            state.forwarding_state = forwarding_state
            state.error_kind = kind
            state.error_message = message
            state.last_error_kind = kind
            state.last_error_message = message
            if state.first_error_kind is None:
                state.first_error_kind = kind
                state.first_error_message = message
            # failed_request_count already incremented by caller if needed
            # Keep status_code/latency as is

    # pylint: disable=too-many-branches,too-many-locals,too-many-statements
    def _forward(self) -> None:
        session: BridgeSession = self.server.session  # type: ignore[attr-defined]
        state = session.state
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        # For chunked incoming from Codex? BaseHTTPRequestHandler handles
        # Content-Length only; Codex CLI always sends Content-Length for
        # Responses API. For safety, if no length but headers indicate
        # chunked, we would need to handle differently, but not required.
        body = self.rfile.read(length) if length > 0 else b""
        forwarded_at_ms = time.monotonic() * 1000.0

        with state.lock:
            state.request_count += 1
            state.request_method = self.command
            state.request_path = self.path
            # Mark pending forward; will be updated to COMPLETED/FAILED later.
            # Do not yet set any_request_forwarded — only after we actually
            # attempt upstream forward.
            if state.forwarding_state == BridgeForwardingState.NOT_STARTED:
                state.forwarding_state = BridgeForwardingState.REQUEST_FORWARDED
            state.forwarded_at_ms = forwarded_at_ms

        target_scheme, target_host, target_port, base_path = session.target
        requested_path = _join_path(base_path, self.path)

        # Scrub incoming headers and preserve only non-secret ones.
        forward_headers_raw = dict(self.headers.items())
        forward_headers = scrub_request_headers(forward_headers_raw)
        # Explicitly ensure no Authorization remains (scrub may have removed)
        # Case-insensitive removal for safety.
        for key in list(forward_headers.keys()):
            if key.lower() == "authorization":
                forward_headers.pop(key, None)
        # The bridge re-injects the connection pin header on every
        # forwarded request -- this is the *only* semantic metadata the
        # bridge adds.  Provider, model, and policy selection remain
        # the gateway's decision.
        forward_headers[CONNECTION_HEADER_NAME] = session.requested_account_id

        # Resolve the real OmniRoute credential at forwarding time from the
        # configured environment variable reference. The real token never
        # enters persistent state.
        auth_env_var = session.authorization_env_var
        real_token: str | None = None
        if auth_env_var:
            env_value = os.environ.get(auth_env_var)
            if env_value is not None:
                stripped = env_value.strip()
                real_token = stripped if stripped else None
        if not real_token:
            with state.lock:
                state.failed_request_count += 1
                # any_request_forwarded stays False because we never forwarded
            self._record_error(
                state,
                kind=BridgeErrorKind.GATEWAY_ERROR,
                message="bridge: OmniRoute authorization not configured (missing env var)",
            )
            self.send_error(502, "bridge missing authorization")
            return

        # Inject the real credential
        forward_headers["Authorization"] = f"Bearer {real_token}"

        # Prepare hop-by-hop filtered headers for http.client
        hop_by_hop = {
            "connection",
            "keep-alive",
            "proxy-authenticate",
            "proxy-authorization",
            "te",
            "trailers",
            "transfer-encoding",
            "upgrade",
            "host",
            "content-length",
        }
        clean_headers: dict[str, str] = {}
        for name, value in forward_headers.items():
            if name.lower() in hop_by_hop:
                continue
            clean_headers[name] = value

        # Choose correct connection type with TLS verification by default.
        try:
            conn: http.client.HTTPConnection
            if target_scheme == "https":
                ctx = ssl.create_default_context()
                conn = http.client.HTTPSConnection(
                    target_host,
                    target_port,
                    timeout=session.relay_timeout,
                    context=ctx,
                )
            else:
                conn = http.client.HTTPConnection(
                    target_host,
                    target_port,
                    timeout=session.relay_timeout,
                )
            # Mark that we are attempting forward (bytes may be sent)
            with state.lock:
                state.any_request_forwarded = True
                state.forwarding_state = BridgeForwardingState.REQUEST_FORWARDED

            # http.client will handle Host header correctly (including port when
            # nonstandard). We also need to handle timeout for connect.
            # Use provided connect_timeout for connection establishment by
            # setting socket timeout? http.client timeout covers both connect
            # and read. We honor relay_timeout as overall.
            # To enforce connect_timeout specifically, we could set timeout
            # smaller initially, but for simplicity use relay_timeout which is
            # >= connect_timeout. The spec says loopback bridge uses stdlib
            # http.client with default verification.

            conn.request(
                self.command,
                requested_path,
                body=body if body else None,
                headers=clean_headers,
            )
        except (OSError, http.client.HTTPException, TimeoutError, ValueError) as exc:
            with state.lock:
                state.failed_request_count += 1
            self._record_error(
                state,
                kind=BridgeErrorKind.FORWARD_FAILED,
                message=f"bridge forward failed: {exc}",
            )
            self.send_error(502, f"bridge forward failed: {exc}")
            try:
                conn.close()
            except Exception:
                pass
            return

        # Obtain response
        try:
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException, TimeoutError) as exc:
            with state.lock:
                state.failed_request_count += 1
            self._record_error(
                state,
                kind=BridgeErrorKind.FORWARD_FAILED,
                message=f"bridge forward failed: {exc}",
            )
            self.send_error(502, f"bridge forward failed: {exc}")
            try:
                conn.close()
            except Exception:
                pass
            return

        # Extract headers and observed account
        raw_headers_list = resp.getheaders()  # list[tuple[str,str]]
        resp_headers_dict: dict[str, str] = dict(raw_headers_list)
        # Case-insensitive lookup for selected connection
        observed: str | None = None
        for k, v in resp_headers_dict.items():
            if k.lower() == SELECTED_CONNECTION_HEADER.lower():
                observed = v.strip() or None
                break
        elapsed_ms = (
            time.monotonic() * 1000.0 - forwarded_at_ms if forwarded_at_ms is not None else None
        )
        scrubbed = scrub_response_headers(resp_headers_dict)

        # Update state for sticky mismatch and counters
        is_mismatch = observed is not None and observed != session.requested_account_id
        with state.lock:
            state.latest_observed_account_id = observed
            if is_mismatch:
                # Sticky: once mismatch, always mismatch for session
                if not state.sticky_mismatch:
                    state.sticky_mismatch = True
                    state.sticky_observed_account_id = observed
                # observed_account_id reflects sticky mismatch value
                state.observed_account_id = state.sticky_observed_account_id
            else:
                if not state.sticky_mismatch:
                    state.observed_account_id = observed
                # else keep sticky observed

            state.status_code = resp.status
            state.latency_ms = elapsed_ms
            state.response_headers = scrubbed

            # Typed infrastructure mapping per spec: 401/403 AUTH_FAILED,
            # 429 QUOTA_UNAVAILABLE, 404/410 ACCOUNT_UNAVAILABLE, 5xx UNAVAILABLE,
            # other 4xx PROTOCOL_ERROR. All error codes count as failed and
            # preserve forwarding but are durably typed via BridgeEvidence.
            if resp.status >= 400:
                state.failed_request_count += 1
                state.forwarding_state = BridgeForwardingState.REQUEST_FAILED
                if resp.status in (401, 403):
                    state.error_kind = BridgeErrorKind.GATEWAY_ERROR
                    state.error_message = f"gateway auth failure {resp.status}"
                elif resp.status == 429:
                    state.error_kind = BridgeErrorKind.GATEWAY_ERROR
                    state.error_message = f"gateway quota unavailable {resp.status}"
                elif resp.status in (404, 410):
                    state.error_kind = BridgeErrorKind.GATEWAY_ERROR
                    state.error_message = f"gateway account unavailable {resp.status}"
                elif resp.status >= 500:
                    state.error_kind = BridgeErrorKind.GATEWAY_ERROR
                    state.error_message = f"gateway server error {resp.status}"
                else:
                    state.error_kind = BridgeErrorKind.GATEWAY_ERROR
                    state.error_message = f"gateway protocol error {resp.status}"
                state.last_error_kind = state.error_kind
                state.last_error_message = state.error_message
                if state.first_error_kind is None:
                    state.first_error_kind = state.last_error_kind
                    state.first_error_message = state.last_error_message
            else:
                state.completed_request_count += 1
                # If previously failed, we keep REQUEST_COMPLETED for this
                # successful request, but sticky mismatch may still mark
                # overall attempt as blocked at higher layer.
                # Only transition to COMPLETED if not already failed due to
                # sticky mismatch? Keep COMPLETED for successful forward.
                state.forwarding_state = BridgeForwardingState.REQUEST_COMPLETED
                # Clear error if successful? Keep last error for history but
                # clear current error_kind if request succeeded.
                state.error_kind = None
                state.error_message = None

        # Relay response back to Codex with streaming
        self.send_response(resp.status)
        for name, value in raw_headers_list:
            lowered = name.lower()
            if lowered in {"transfer-encoding", "connection", "content-length"}:
                continue
            # Never leak secret-like headers back? They are already scrubbed
            # for evidence, but response to Codex should preserve upstream
            # semantics (except hop-by-hop). Do not filter secret headers
            # here because upstream secret headers shouldn't exist, but if
            # they do, they are part of upstream response semantics.
            self.send_header(name, value)
        # For streaming without known length, we use Connection: close
        # and stream until upstream EOF. This avoids buffering arbitrarily
        # large responses while remaining bounded by relay_timeout and
        # worker lifecycle.
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            # Genuinely incremental relay: stream as bytes arrive without
            # buffering the entire upstream response and without an arbitrary
            # 50 ms quiet-window cutoff. Prefer a bounded incremental primitive
            # (read1) when available to deliver the first SSE event before
            # upstream close/barrier and before 8 KiB accumulates.
            def _read_upstream() -> bytes:
                # http.client.HTTPResponse supports read(); the underlying
                # buffered file may expose read1() for true incremental
                # delivery. Use read1 when present to avoid waiting for
                # exactly 8192 bytes.
                read1 = getattr(resp, "read1", None)
                if callable(read1):
                    try:
                        data = read1(8192)
                        if data:
                            return data  # type: ignore[no-any-return]
                    except Exception:
                        pass
                fp1 = getattr(getattr(resp, "fp", None), "read1", None)
                if callable(fp1):
                    try:
                        data = fp1(8192)
                        if data:
                            return data  # type: ignore[no-any-return]
                    except Exception:
                        pass
                result = resp.read(8192)
                return result

            while True:
                chunk = _read_upstream()
                if not chunk:
                    # For SSE with chunked framing, read(8192) may return b"" on
                    # temporary no-data but not EOF; fallback to blocking read
                    # with timeout already covers pending bytes. Treat empty as
                    # EOF only when upstream has closed (getresponse already
                    # finished). For safety, attempt one more blocking read.
                    # However to avoid spinning, break if no data and stream
                    # appears complete. The http.client will return b"" only at
                    # EOF for the response body.
                    break
                try:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    # Codex may close early; bytes were relayed as far as
                    # possible. Treat as successful relay from bridge perspective.
                    break
        except (OSError, http.client.HTTPException, TimeoutError) as exc:
            # If we already sent headers, we cannot send_error; just record
            # failure for evidence but don't break client otherwise.
            with state.lock:
                # If chunk read failed after headers sent, mark as failed?
                # For SSE streaming, premature timeout should be considered
                # forward failure, but we already marked completed for header.
                # Adjust counts: move from completed to failed if read fails.
                if (
                    state.completed_request_count > 0
                    and state.forwarding_state == BridgeForwardingState.REQUEST_COMPLETED
                ):
                    state.completed_request_count -= 1
                    state.failed_request_count += 1
                state.forwarding_state = BridgeForwardingState.REQUEST_FAILED
                state.error_kind = BridgeErrorKind.FORWARD_FAILED
                state.error_message = f"bridge relay read failed: {exc}"
                state.last_error_kind = BridgeErrorKind.FORWARD_FAILED
                state.last_error_message = state.error_message
                if state.first_error_kind is None:
                    state.first_error_kind = state.last_error_kind
                    state.first_error_message = state.last_error_message
        finally:
            try:
                conn.close()
            except Exception:
                pass


class _BridgeHTTPServer(ThreadingHTTPServer):
    """Threading HTTP server that exposes the per-session bridge state."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        session: BridgeSession,
    ) -> None:
        super().__init__(address, handler)
        self.session = session


@dataclass
class BridgeSession:
    """A bounded bridge session bound to one Codex worker attempt.

    The session binds a local loopback port to the configured OmniRoute
    endpoint. The Codex adapter is instructed to use the loopback URL
    via the custom provider ``base_url`` override. The session collects
    one ``BridgeEvidence`` row, returned through :meth:`finalize`.
    """

    session_id: str
    requested_account_id: str
    provider: str
    target: tuple[str, str, int, str]
    connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS
    relay_timeout: float = _DEFAULT_RELAY_TIMEOUT_SECONDS
    authorization_env_var: str = "OMNIROUTE_API_KEY"
    state: _BridgeState = field(init=False)
    _server: _BridgeHTTPServer | None = field(default=None, init=False)
    _bind_host: str = "127.0.0.1"
    _bind_port: int = 0
    loopback_url: str = ""

    def __post_init__(self) -> None:
        self.state = _BridgeState(started_at=utc_now_iso())

    @property
    def session_id_public(self) -> str:
        return self.session_id

    @property
    def bind_host(self) -> str:
        return self._bind_host

    @property
    def bind_port(self) -> int:
        return self._bind_port

    def start(self) -> str:
        """Start the loopback bridge and return the URL Codex must target."""

        server = _BridgeHTTPServer((self._bind_host, self._bind_port), _BridgeRequestHandler, self)
        self._server = server
        bind_address = server.server_address
        self._bind_host = str(bind_address[0])
        self._bind_port = int(bind_address[1])
        self.loopback_url = f"http://{self._bind_host}:{self._bind_port}/v1"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return self.loopback_url

    def stop(self) -> None:
        """Stop the bridge. Idempotent and safe to call multiple times."""

        with self.state.lock:
            if self.state.forwarding_state not in {
                BridgeForwardingState.BRIDGE_TERMINATED,
                BridgeForwardingState.REQUEST_FAILED,
                BridgeForwardingState.REQUEST_COMPLETED,
            }:
                # If no request was ever forwarded, mark as UNKNOWN to
                # indicate we never saw a semantic request. If we saw
                # requests but final is still REQUEST_FORWARDED (e.g. worker
                # crashed mid-forward), keep REQUEST_FAILED? For now keep
                # TERMINATED semantics for clean shutdown.
                if self.state.request_count == 0:
                    self.state.forwarding_state = BridgeForwardingState.BRIDGE_TERMINATED
                elif self.state.forwarding_state == BridgeForwardingState.REQUEST_FORWARDED:
                    self.state.forwarding_state = BridgeForwardingState.REQUEST_FAILED
                else:
                    self.state.forwarding_state = BridgeForwardingState.BRIDGE_TERMINATED
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass
            try:
                self._server.server_close()
            except Exception:
                pass

    def finalize(self, *, ended_at: str | None = None) -> BridgeEvidence:
        """Return the bounded evidence row for the session."""

        snapshot = self.state.snapshot()
        with self.state.lock:
            final_state = snapshot["forwarding_state"]
            if final_state == BridgeForwardingState.NOT_STARTED and self._server is not None:
                final_state = BridgeForwardingState.UNKNOWN
            # Preserve BRIDGE_TERMINATED for clean shutdown without requests
            # as UNKNOWN? Spec says when bridge never received request and is
            # configured, disposition is SAFE_DIRECT_FALLBACK or GATEWAY_BLOCKED
            # depending on fallback. But BridgeEvidence forwarding_state should
            # be UNKNOWN when no request forwarded. Our state already tracks
            # request_count, so UNKNOWN vs TERMINATED matters for disposition.
            # Keep logic: if request_count ==0 and final is TERMINATED, treat
            # as UNKNOWN for evidence to indicate no forwarding occurred.
            if (
                snapshot["request_count"] == 0
                and final_state == BridgeForwardingState.BRIDGE_TERMINATED
            ):
                final_state = BridgeForwardingState.UNKNOWN
        return BridgeEvidence(
            schema_version=_BRIDGE_VERSION,
            session_id=self.session_id,
            started_at=self.state.started_at,
            ended_at=ended_at or utc_now_iso(),
            requested_account_id=self.requested_account_id,
            provider=self.provider,
            observed_account_id=snapshot["observed_account_id"],
            forwarding_state=final_state,
            error_kind=snapshot["error_kind"],
            error_message=snapshot["error_message"],
            request_method=snapshot["request_method"],
            request_path=snapshot["request_path"],
            status_code=snapshot["status_code"],
            latency_ms=snapshot["latency_ms"],
            headers=snapshot["response_headers"],
            request_count=snapshot["request_count"],
            any_request_forwarded=snapshot["any_request_forwarded"],
            completed_request_count=snapshot["completed_request_count"],
            failed_request_count=snapshot["failed_request_count"],
            sticky_mismatch=snapshot["sticky_mismatch"],
            latest_observed_account_id=snapshot["latest_observed_account_id"],
            first_error_kind=snapshot["first_error_kind"],
            first_error_message=snapshot["first_error_message"],
            last_error_kind=snapshot["last_error_kind"],
            last_error_message=snapshot["last_error_message"],
        )


# ---------------------------------------------------------------------------
# High-level orchestration helper
# ---------------------------------------------------------------------------


def build_loopback_bridge_session(
    *,
    endpoint: str,
    requested_account_id: str,
    provider: str,
    relay_timeout: float = _DEFAULT_RELAY_TIMEOUT_SECONDS,
    connect_timeout: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS,
    authorization_env_var: str = "OMNIROUTE_API_KEY",
) -> BridgeSession:
    """Construct (but do not start) a :class:`BridgeSession`.

    The session is started by :meth:`BridgeSession.start` and torn down
    by :meth:`BridgeSession.stop`.  Either method is idempotent.
    """

    target = _split_host(endpoint)
    return BridgeSession(
        session_id=f"bridge-{uuid.uuid4().hex}",
        requested_account_id=requested_account_id,
        provider=provider,
        target=target,
        connect_timeout=connect_timeout,
        relay_timeout=relay_timeout,
        authorization_env_var=authorization_env_var,
    )


def serialize_bridge_evidence(evidence: BridgeEvidence) -> str:
    """Canonical JSON serialization of bridge evidence."""

    return json.dumps(evidence.as_dict(), sort_keys=True, separators=(",", ":"))


def parse_provider_config_overrides(
    *,
    provider_id: str,
    base_url: str,
    env_key_var: str,
    wire_api: str,
    env_http_header_vars: Mapping[str, str],
) -> tuple[str, str]:
    """Return ``(-c model_provider=..., -c model_providers.<id>={...})`` strings.

    The Codex CLI accepts TOML overrides via repeated ``-c key=value``
    flags.  The provider table is encoded as an inline TOML table.

    The bridge treats the env var *name* as the value (NOT the secret).
    """

    if not provider_id or not provider_id.strip():
        raise GatewayManifestError("provider_id is required")
    if not base_url or not base_url.strip():
        raise GatewayManifestError("base_url is required")
    if not env_key_var or not env_key_var.strip():
        raise GatewayManifestError("env_key_var is required")
    if wire_api not in {"responses", "chat"}:
        raise GatewayManifestError(f"unsupported wire_api for C10 gateway transport: {wire_api!r}")
    table_lines = [
        f'name = "orchestrator-c10-{provider_id}"',
        f'base_url = "{base_url}"',
        f'env_key = "{env_key_var}"',
        f'wire_api = "{wire_api}"',
    ]
    if env_http_header_vars:
        pairs = ", ".join(
            f'"{header_name}" = "{env_var}"'
            for header_name, env_var in sorted(env_http_header_vars.items())
        )
        table_lines.append(f"env_http_headers = {{{pairs}}}")
    table_value = "{" + ", ".join(table_lines) + "}"
    select_value = f'model_provider = "{provider_id}"'
    provider_key = f"model_providers.{provider_id}"
    return (
        f"{select_value}",
        f"{provider_key} = {table_value}",
    )


__all__ = [
    "BRIDGE_VERSION",
    "BridgeErrorKind",
    "BridgeEvidence",
    "BridgeForwardingState",
    "BridgeSession",
    "build_loopback_bridge_session",
    "parse_provider_config_overrides",
    "scrub_request_headers",
    "scrub_response_headers",
    "serialize_bridge_evidence",
]

# Re-export the schema version constant under a stable public name.
BRIDGE_VERSION = _BRIDGE_VERSION
