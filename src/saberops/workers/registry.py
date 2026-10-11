"""Registry for provider worker adapters."""

from __future__ import annotations

from saberops.workers.antigravity import AntigravityAdapter
from saberops.workers.base import WorkerAdapter
from saberops.workers.claude import ClaudeCodeAdapter
from saberops.workers.cline import ClineAdapter
from saberops.workers.codex import CodexAdapter
from saberops.workers.opencode import OpenCodeAdapter


def attended_adapter(provider: str) -> WorkerAdapter | None:
    """Return the adapter for an attended-only provider, if any.

    Attended-only providers (ADR-0001 Amendment 1: Antigravity) are
    never auto-included in :meth:`AdapterRegistry.default` and never
    appear in automatic routing chains.  They resolve exclusively
    through this explicit lookup, reached only when the owner
    explicitly overrides provider+model for a run.  Returns ``None``
    for any other provider name.
    """
    if provider.strip().lower() == "antigravity":
        return AntigravityAdapter()
    return None


class AdapterRegistry:
    """Registry mapping provider names to WorkerAdapter instances."""

    def __init__(self, adapters: list[WorkerAdapter] | None = None) -> None:
        self._adapters: dict[str, WorkerAdapter] = {}
        if adapters is not None:
            for adapter in adapters:
                self.register(adapter)

    def register(self, adapter: WorkerAdapter) -> None:
        """Register a worker adapter."""
        self._adapters[adapter.provider_name.lower()] = adapter

    def get(self, provider: str) -> WorkerAdapter | None:
        """Get the adapter for a given provider name, or None if not registered.

        Attended-only providers (Antigravity) are resolved through the
        explicit :func:`attended_adapter` lookup rather than the
        auto-included set, so an explicit owner provider+model override
        still dispatches while automatic enumeration never offers them.
        """
        existing = self._adapters.get(provider.lower())
        if existing is not None:
            return existing
        explicit = attended_adapter(provider)
        if explicit is not None:
            return explicit
        from saberops.workers.direct_api import (
            DirectAPIAdapter,
            api_connections,
            supports_connection,
        )

        if any(
            c.provider == provider.lower() and supports_connection(c) for c in api_connections()
        ):
            return DirectAPIAdapter(provider.lower())
        return None

    def is_available(self, provider: str) -> bool:
        """Check if adapter for provider is registered and locally available."""
        adapter = self.get(provider)
        return adapter is not None and adapter.is_available()

    def get_registered_providers(self) -> list[str]:
        """Return list of all registered provider names."""
        return list(self._adapters.keys())

    def get_available_providers(self) -> list[str]:
        """Return list of all currently available provider names."""
        return [name for name, adapter in self._adapters.items() if adapter.is_available()]

    @classmethod
    def default(cls) -> AdapterRegistry:
        """Create a default registry populated with standard provider adapters.

        Attended-only providers are never auto-included: Antigravity
        resolves exclusively through :func:`attended_adapter` on an
        explicit owner override (ADR-0001 Amendment 1, item 3), so no
        automatic chain can ever select it.
        """
        return cls(
            adapters=[
                OpenCodeAdapter(),
                CodexAdapter(),
                ClineAdapter(),
                ClaudeCodeAdapter(),
            ]
        )
