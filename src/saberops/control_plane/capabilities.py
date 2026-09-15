"""Factual provider and adapter capability vocabulary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ProviderTransport(StrEnum):
    DIRECT_CLI = "DIRECT_CLI"
    DIRECT_API = "DIRECT_API"
    GATEWAY = "GATEWAY"


class Capability(StrEnum):
    STREAMING = "streaming"
    LARGE_PROMPT_STDIN = "large_prompt_stdin"
    RESUME = "resume"
    SESSION_ID = "session_id"
    READ_ONLY = "read_only"
    STRUCTURED_USAGE = "structured_usage"
    REASONING_EFFORT = "reasoning_effort"
    OUTPUT_SCHEMA = "output_schema"
    PROCESS_IDENTITY = "process_identity"
    CANCELLATION = "cancellation"
    TOOL_RESTRICTION = "tool_restriction"
    HOST_ACCESS = "host_access"


def normalize_required_capabilities(
    values: Iterable[str | Capability],
) -> tuple[str, ...]:
    """Validate and canonicalize a run's required factual capabilities."""
    normalized: set[str] = set()
    for raw in values:
        value = raw.value if isinstance(raw, Capability) else str(raw).strip().lower()
        try:
            normalized.add(Capability(value).value)
        except ValueError as exc:
            raise ValueError(f"unknown required capability: {value}") from exc
    return tuple(sorted(normalized))


class GatewayCapability(StrEnum):
    SUPPORTS_ACCOUNTS = "supports_accounts"
    QUOTA = "quota"
    HEALTH = "health"
    CIRCUIT_BREAKER = "circuit_breaker"
    SESSION_AFFINITY = "session_affinity"
    COMPRESSION = "compression"


@dataclass(frozen=True)
class CapabilityProfile:
    provider: str
    transport: ProviderTransport
    capabilities: frozenset[Capability] = frozenset()
    gateway_capabilities: frozenset[GatewayCapability] = frozenset()
    digest: str = ""

    def __post_init__(self) -> None:
        provider = self.provider.strip().lower()
        object.__setattr__(self, "provider", provider)
        if not self.digest:
            payload = {
                "provider": provider,
                "transport": self.transport.value,
                "capabilities": sorted(c.value for c in self.capabilities),
                "gateway_capabilities": sorted(c.value for c in self.gateway_capabilities),
            }
            raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            object.__setattr__(self, "digest", hashlib.sha256(raw).hexdigest())

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "transport": self.transport.value,
            "capabilities": sorted(c.value for c in self.capabilities),
            "gateway_capabilities": sorted(c.value for c in self.gateway_capabilities),
            "digest": self.digest,
        }


# These are deliberately limited to behavior implemented by the adapters.
_PROFILES: dict[str, CapabilityProfile] = {
    "opencode": CapabilityProfile(
        "opencode",
        ProviderTransport.DIRECT_CLI,
        frozenset(
            {
                Capability.STREAMING,
                Capability.LARGE_PROMPT_STDIN,
                Capability.STRUCTURED_USAGE,
                Capability.PROCESS_IDENTITY,
                Capability.CANCELLATION,
            }
        ),
    ),
    "codex": CapabilityProfile(
        "codex",
        ProviderTransport.DIRECT_CLI,
        frozenset(
            {
                Capability.STREAMING,
                Capability.LARGE_PROMPT_STDIN,
                Capability.READ_ONLY,
                Capability.STRUCTURED_USAGE,
                Capability.PROCESS_IDENTITY,
                Capability.CANCELLATION,
            }
        ),
    ),
    "cline": CapabilityProfile(
        "cline",
        ProviderTransport.DIRECT_CLI,
        frozenset(
            {
                Capability.STREAMING,
                Capability.STRUCTURED_USAGE,
                Capability.PROCESS_IDENTITY,
                Capability.CANCELLATION,
            }
        ),
    ),
    "antigravity": CapabilityProfile(
        "antigravity",
        ProviderTransport.DIRECT_CLI,
        frozenset({Capability.STREAMING, Capability.PROCESS_IDENTITY, Capability.CANCELLATION}),
    ),
}


def capability_profile_for(
    adapter: Any | None = None, provider: str | None = None
) -> CapabilityProfile:
    """Return a stable, read-only profile without contacting a provider."""
    name = provider or getattr(adapter, "provider_name", "")
    normalized = str(name).strip().lower()
    descriptor = getattr(adapter, "capability_profile_descriptor", None)
    if callable(descriptor):
        try:
            raw = descriptor()
        except Exception:  # noqa: BLE001 - intentional: never trust a throwing descriptor
            # A descriptor which raises is treated as an absent
            # descriptor: it must never silently fall back to the
            # provider-wide profile, because a binding's selection
            # adapter uses this seam to declare what *this* binding
            # actually carries -- not what the provider can do in
            # general.
            raw = None
        if isinstance(raw, dict):
            try:
                return CapabilityProfile(
                    normalized,
                    ProviderTransport(str(raw.get("transport", "DIRECT_CLI"))),
                    frozenset(Capability(str(item)) for item in raw.get("capabilities", [])),
                    frozenset(
                        GatewayCapability(str(item)) for item in raw.get("gateway_capabilities", [])
                    ),
                )
            except (TypeError, ValueError):
                # An invalid adapter declaration is treated as no declared
                # capability, never as stronger trust.
                return CapabilityProfile(normalized, ProviderTransport.DIRECT_CLI)
        # The descriptor raised or returned a non-dict payload; treat
        # it as if no descriptor had been supplied at all rather than
        # silently inheriting the provider-wide profile.
        return CapabilityProfile(normalized, ProviderTransport.DIRECT_CLI)
    profile = _PROFILES.get(normalized)
    if profile is not None:
        return profile
    # Unknown adapters are not granted factual capabilities.
    return CapabilityProfile(normalized, ProviderTransport.DIRECT_CLI)
