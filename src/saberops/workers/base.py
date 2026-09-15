"""Provider-neutral worker adapter interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from saberops.models import WorkerRequest, WorkerResult


def streaming_process_kwargs(request: WorkerRequest) -> dict[str, Any]:
    """Build run_process kwargs for live output and activity-aware timeouts.

    Adapters merge this mapping into their ``run_process`` call so provider
    stdout/stderr is streamed to the caller's callbacks while the worker is
    still running, and the soft inactivity threshold (never a kill boundary)
    is evaluated against real process output.  Keys are only present when the
    corresponding feature was requested, so callers that ignore them keep the
    previous capture-until-completion behavior.
    """
    kwargs: dict[str, Any] = {}
    kwargs["inherit_env_exclude"] = request.inherit_env_exclude
    if request.on_stdout is not None:
        kwargs["on_stdout"] = request.on_stdout
    if request.on_stderr is not None:
        kwargs["on_stderr"] = request.on_stderr
    if request.inactivity_timeout is not None:
        kwargs["inactivity_timeout"] = request.inactivity_timeout
    if request.on_stall is not None:
        kwargs["on_stall"] = request.on_stall
    if request.on_resume is not None:
        kwargs["on_resume"] = request.on_resume
    if request.on_process_start is not None:
        kwargs["on_process_start"] = request.on_process_start
    if request.on_monitor_transition is not None:
        kwargs["on_monitor_transition"] = request.on_monitor_transition
    return kwargs


class WorkerAdapter(ABC):
    """Abstract base class for worker execution adapters."""

    # Provider modules publish these primitive descriptors without importing
    # control-plane policy code.  The control plane turns them into its typed,
    # hashed CapabilityProfile on read.
    capability_transport: str = "DIRECT_CLI"
    factual_capabilities: frozenset[str] = frozenset()
    gateway_capabilities: frozenset[str] = frozenset()
    # C10-R3: bounded list of gateway-transport capabilities the adapter has
    # been IMPLEMENTED AND TESTED for.  Distinct from ``gateway_capabilities``
    # above, which the control plane coerces through the GatewayCapability
    # enum (a C07 surface).  An adapter advertises a transport only after it
    # has been proven to consume ``ORCH_GATEWAY_*`` env vars and route its
    # real HTTP requests through the configured OmniRoute transport.
    c10_gateway_transport_capabilities: frozenset[str] = frozenset()

    # C11-C.8: True when the backend inherently needs provider network
    # egress to function (every hosted-model CLI does).  Containment
    # cannot separate provider egress from worker/tool egress, so an
    # autonomous dispatch that requires provider network fails closed
    # instead of running with unenforced isolation.  Local-execution
    # backends that need no egress set this False.
    requires_provider_network: bool = True

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the worker provider (e.g. 'opencode')."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if worker CLI / environment is available locally."""
        ...

    @abstractmethod
    def run(self, request: WorkerRequest) -> WorkerResult:
        """Execute the task in the requested isolated worktree."""
        ...

    def max_prompt_bytes(self) -> int | None:
        """Return the adapter's verified prompt transport ceiling in bytes.

        Return ``None`` when the adapter has a verified large-prompt transport
        (for example Codex's stdin fallback) with no byte ceiling the caller
        must enforce. Return a concrete integer for the adapter's verified argv
        capability: prompts larger than this cannot be delivered by the adapter
        and must be preflight-skipped by callers instead of launching a doomed
        process.
        """
        return None

    def prompt_transport(self) -> str:
        """Return the adapter's verified transport CAPABILITY descriptor.

        This is a single, size-independent label (``"argv"``, ``"stdin"``, or
        ``"unknown"``) describing what large-prompt fallback the adapter has
        verified, if any -- it is NOT the transport a specific dispatch will
        actually use. An adapter with a verified stdin fallback (OpenCode,
        Codex) still uses argv for small prompts; use
        :meth:`prompt_transport_for` to resolve the transport a given
        dispatch will actually take before launch.
        """
        return "unknown"

    def prompt_transport_for(self, prompt_bytes: int) -> str:
        """Return the PLANNED transport for a dispatch of this exact size.

        Deterministic and size-aware, resolvable before launch (no process
        started, no model tokens spent). Distinct from
        :meth:`prompt_transport`, which reports capability, not per-call
        choice: a single-transport adapter (e.g. argv-only Antigravity)
        returns the same descriptor regardless of size, so the default
        implementation delegates to :meth:`prompt_transport`. Adapters with
        a size-dependent argv/stdin split (OpenCode, Codex) override this to
        report the transport their own dispatch logic will actually take.
        Once a call completes, prefer the adapter's actual
        ``WorkerResult.prompt_transport_used`` over this planned value --
        this method exists for PRE-dispatch telemetry (e.g. ``worker_started``)
        where no result exists yet.
        """
        return self.prompt_transport()

    def capability_profile_descriptor(self) -> dict[str, object]:
        """Return read-only factual capability data; never contacts a provider."""
        return {
            "provider": self.provider_name,
            "transport": self.capability_transport,
            "capabilities": sorted(self.factual_capabilities),
            "gateway_capabilities": sorted(self.gateway_capabilities),
        }

    def capability_profile(self) -> dict[str, object]:
        """Public adapter profile surface consumed by the control plane."""
        return self.capability_profile_descriptor()
