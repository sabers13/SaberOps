"""Registry for provider worker adapters."""

from __future__ import annotations

from orchestrator_mvp.workers.antigravity import AntigravityAdapter
from orchestrator_mvp.workers.base import WorkerAdapter
from orchestrator_mvp.workers.cline import ClineAdapter
from orchestrator_mvp.workers.codex import CodexAdapter
from orchestrator_mvp.workers.copilot import CopilotAdapter
from orchestrator_mvp.workers.opencode import OpenCodeAdapter


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
        """Get the adapter for a given provider name, or None if not registered."""
        return self._adapters.get(provider.lower())

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

        Copilot is included as a backend even when its executable is not
        installed -- the registry simply reports ``is_available() == False``
        for it; this keeps it structurally optional everywhere else.
        """
        return cls(
            adapters=[
                OpenCodeAdapter(),
                CodexAdapter(),
                AntigravityAdapter(),
                ClineAdapter(),
                CopilotAdapter(),
            ]
        )
