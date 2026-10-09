"""Explicit, non-inference API verification at the credential boundary."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener

from saberops.model_access.connections import ProviderConnection
from saberops.model_access.readiness import ProviderReadinessEvidence, ReadinessEvidenceSource
from saberops.models import ProviderReadiness
from saberops.workers.api_agent import NoRedirect, completion_url
from saberops.workers.direct_api import credential_value, supports_connection


def verify_api(connection: ProviderConnection) -> ProviderReadinessEvidence:
    state, reason = ProviderReadiness.NOT_READY, "CONFIG_INVALID"
    if not connection.enabled:
        reason = "CONNECTION_DISABLED"
    elif not supports_connection(connection):
        reason = "PROTOCOL_UNSUPPORTED"
    else:
        try:
            credential = credential_value(connection)
        except ValueError:
            reason = "CREDENTIAL_MISSING"
        else:
            parts = urlsplit(completion_url(connection.endpoint))
            openrouter = parts.hostname == "openrouter.ai"
            path = (
                "/api/v1/key"
                if openrouter
                else parts.path.removesuffix("/chat/completions") + "/models"
            )
            url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
            headers = dict(connection.public_headers)
            headers["Accept"] = "application/json"
            if credential:
                name = connection.auth_header_name or "Authorization"
                headers[name] = (
                    f"Bearer {credential}" if name.lower() == "authorization" else credential
                )
            try:
                with build_opener(NoRedirect()).open(
                    Request(url, headers=headers), timeout=8
                ) as response:
                    raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise ValueError("oversized metadata")
                payload = json.loads(raw)
                data = payload.get("data") if isinstance(payload, dict) else None
                if (openrouter and not isinstance(data, dict)) or (
                    not openrouter and not isinstance(data, list)
                ):
                    raise ValueError("invalid metadata")
                state = ProviderReadiness.READY
                # A generic models catalog proves endpoint readiness, not that
                # a public endpoint validated credentials or model entitlement.
                reason = "READY" if openrouter else "API_ENDPOINT_READY"
            except HTTPError as exc:
                reason = "CREDENTIAL_REJECTED" if exc.code in (401, 403) else "ENDPOINT_UNREACHABLE"
            except (OSError, ValueError):
                reason = "ENDPOINT_UNREACHABLE"
    return ProviderReadinessEvidence(
        connection_id=connection.connection_id,
        provider=connection.provider,
        backend=connection.backend,
        state=state,
        reason=reason,
        observed_at=datetime.now(UTC).isoformat(),
        source=ReadinessEvidenceSource.CONNECTION_TEST,
    )
