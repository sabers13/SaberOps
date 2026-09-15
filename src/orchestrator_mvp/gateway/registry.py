"""Provider-neutral gateway registry and dispatch surface.

The registry is a small lookup keyed by :class:`GatewayKind`. Direct
execution is the first-class ``DIRECT`` entry; OmniRoute is the only
implemented third-party backend. New backends may be added without
changing the dispatch contract.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from orchestrator_mvp.gateway.contracts import (
    FrozenGatewayConfig,
    GatewayAccount,
    GatewayDispatchResult,
    GatewayKind,
    GatewayManifestError,
)
from orchestrator_mvp.gateway.omniroute import OmniRouteBackendConfig, OmniRouteGateway

GatewayFactory = Callable[..., Any]


@dataclass(frozen=True)
class DirectGateway:
    """The first-class, no-gateway execution path.

    ``DirectGateway`` is the implementation of the ``DIRECT`` kind. It
    is not a network proxy; it represents the decision *not* to route
    through any gateway and to fall back to the existing per-provider
    direct worker adapter path.
    """

    enabled: bool = False

    @property
    def kind(self) -> GatewayKind:
        return GatewayKind.DIRECT

    def dispatch(
        self,
        *,
        provider: str,
        model: str,
    ) -> GatewayDispatchResult:
        """Return a typed no-op ``DIRECT`` result.

        The actual worker invocation is performed by the existing
        :class:`WorkerAdapter` machinery; this method only emits the
        infrastructure evidence that *no* gateway was used.
        """
        if not self.enabled:
            return GatewayDispatchResult(
                success=True,
                kind=GatewayKind.DIRECT,
                requested_provider=provider,
                requested_model=model,
                evidence={"gateway_used": False, "direct": True},
            )
        return GatewayDispatchResult(
            success=True,
            kind=GatewayKind.DIRECT,
            requested_provider=provider,
            requested_model=model,
            evidence={"gateway_used": False, "direct": True},
        )


@dataclass(frozen=True)
class GatewayRegistryEntry:
    """One registered gateway backend."""

    kind: GatewayKind
    factory: GatewayFactory | None
    config_digest: str
    enabled: bool

    def __post_init__(self) -> None:
        if self.kind == GatewayKind.DIRECT and self.enabled:
            raise GatewayManifestError("DIRECT gateway cannot be registered as enabled")


class GatewayRegistry:
    """In-process registry of available gateway backends.

    The registry is constructed once and then read-only. Registration is
    explicit administrative input; nothing is auto-discovered from the
    environment.
    """

    def __init__(self, entries: Iterable[GatewayRegistryEntry] = ()) -> None:
        self._entries: dict[GatewayKind, GatewayRegistryEntry] = {}
        for entry in entries:
            self._register(entry)
        if GatewayKind.DIRECT not in self._entries:
            # DIRECT is always available as the first-class no-gateway path.
            self._register(
                GatewayRegistryEntry(
                    kind=GatewayKind.DIRECT,
                    factory=None,
                    config_digest="direct",
                    enabled=False,
                )
            )

    def _register(self, entry: GatewayRegistryEntry) -> None:
        if entry.kind in self._entries:
            raise GatewayManifestError(f"duplicate gateway registration: {entry.kind.value}")
        self._entries[entry.kind] = entry

    @property
    def kinds(self) -> tuple[GatewayKind, ...]:
        return tuple(sorted(self._entries.keys(), key=lambda kind: kind.value))

    def get(self, kind: GatewayKind) -> GatewayRegistryEntry | None:
        return self._entries.get(kind)

    def enabled(self, kind: GatewayKind) -> bool:
        entry = self._entries.get(kind)
        if entry is None:
            return False
        return entry.enabled

    @classmethod
    def default(
        cls,
        *,
        omniroute: OmniRouteBackendConfig | None = None,
        omniroute_accounts: tuple[GatewayAccount, ...] = (),
        omniroute_transport: Mapping[str, object] | None = None,
    ) -> GatewayRegistry:
        """Return a registry preloaded with DIRECT plus, optionally, OmniRoute."""
        entries: list[GatewayRegistryEntry] = []
        if omniroute is not None:
            from orchestrator_mvp.gateway.omniroute import freeze_omniroute_config

            frozen = freeze_omniroute_config(
                omniroute,
                enabled=False,
                accounts=omniroute_accounts,
            )
            entries.append(
                GatewayRegistryEntry(
                    kind=GatewayKind.OMNIROUTE,
                    factory=OmniRouteGateway,
                    config_digest=frozen.digest,
                    enabled=False,
                )
            )
        return cls(entries)


def resolve_dispatch_kind(
    *,
    requested: GatewayKind,
    frozen: FrozenGatewayConfig | None,
) -> GatewayKind:
    """Return the dispatch kind, falling back to ``DIRECT`` when no gateway is enabled.

    This is a small helper used by the orchestrator: if the registry has
    no enabled gateway for ``requested`` (or the run's frozen contract
    has ``enabled=False``), the dispatch kind collapses to ``DIRECT``.
    """
    if frozen is None:
        return GatewayKind.DIRECT
    if not frozen.enabled:
        return GatewayKind.DIRECT
    if frozen.kind != requested:
        return GatewayKind.DIRECT
    return requested


def build_direct_dispatch(
    *,
    provider: str,
    model: str,
) -> GatewayDispatchResult:
    """Build a typed evidence-only result for a direct dispatch."""
    return GatewayDispatchResult(
        success=True,
        kind=GatewayKind.DIRECT,
        requested_provider=provider,
        requested_model=model,
        evidence={"gateway_used": False, "direct": True},
    )


__all__ = [
    "DirectGateway",
    "GatewayFactory",
    "GatewayRegistry",
    "GatewayRegistryEntry",
    "build_direct_dispatch",
    "resolve_dispatch_kind",
]
