"""Owner routing edits for the web UI (one job at a time).

The Routing page answers "which model does each job, and who takes over
when it is busy".  This module is the single seam between that page and
the two existing routing authorities:

* the schema-3 routing document (``routing_config``) for the WORKER,
  SURGEON and INTEGRATOR fallback chains, written only through
  :func:`save_user_routing` after the canonical validators accept it;
* the owner binding store for the REVIEWER fallback order, written only
  after the real reviewer resolver accepts the exact binding.

A model becomes routable only through evidence: a discovery-verified,
fresh, execution-supported catalog entry whose exact WORKER binding
exists and resolves through the real adapter.  The approval record
written next to the chain is the canonical :class:`DynamicCandidate`
contract, so restarts and frozen run snapshots recover the same exact
binding.  Nothing here invents a model id, a binding or a default.

The Orchestrator ("Plans the work") is never chosen here: it is the
agent connected over MCP, read from the writer lease.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Any

from saberops.control_plane import (
    BindingRole,
    OwnerBindings,
    save_owner_bindings,
)
from saberops.control_plane.bindings import (
    BindingKind,
    BindingResolutionError,
    resolve_execution_binding,
)
from saberops.control_plane.reviewer_binding import (
    ReviewerSelectionStatus,
    select_reviewer_binding,
)
from saberops.model_access.discovery import ModelCatalog
from saberops.routing_config import (
    ALLOWED_CANDIDATES,
    DynamicCandidate,
    RoutingConfigError,
    add_candidate_to_chain,
    format_candidate_id,
    load_effective_routing_payload,
    move_group_in_chain,
    remove_candidate_from_chain,
    requires_training_permission,
    save_user_routing,
)
from saberops.workers.registry import AdapterRegistry

__all__ = [
    "JOBS",
    "Job",
    "RoutingEditError",
    "RoutingOption",
    "add_to_job",
    "chain_options",
    "job_by_slug",
    "move_in_job",
    "remove_from_job",
    "reviewer_options",
]

_APPROVED_KEY = "approved_candidates"
_mutation_lock = threading.Lock()


class RoutingEditError(ValueError):
    """An owner routing edit was refused; nothing was written."""


@dataclass(frozen=True)
class Job:
    """One job on the Routing page, in the order work flows through them."""

    slug: str
    role: str  # engine role: ORCHESTRATOR, WORKER, REVIEWER, SURGEON, INTEGRATOR
    name: str
    verb: str
    help: str

    @property
    def store(self) -> str:
        """Where the chain lives: the agent lease, the binding store or routing.json."""
        if self.role == "ORCHESTRATOR":
            return "agent"
        return "reviewer" if self.role == "REVIEWER" else "routing"


JOBS: tuple[Job, ...] = (
    Job("plan", "ORCHESTRATOR", "Plans the work", "plans",
        "Breaks a request into tasks. This is always the agent you connect to SaberOps."),
    Job("write", "WORKER", "Writes the code", "writes",
        "Makes each change in its own safe copy of the project."),
    Job("check", "REVIEWER", "Checks the work", "checks",
        "Reads every change before you see it. Pick a model from a different "
        "company than the one that writes the code."),
    Job("repair", "SURGEON", "Repairs failed work", "repairs",
        "Takes over when a change fails its checks."),
    Job("combine", "INTEGRATOR", "Combines finished work", "combines",
        "Joins accepted changes so they fit together."),
)


def job_by_slug(slug: str) -> Job | None:
    return next((job for job in JOBS if job.slug == slug.strip().lower()), None)


@dataclass(frozen=True)
class RoutingOption:
    """One model the owner may put on a job, with its exact identity."""

    key: str  # routing: candidate id; reviewer: binding id
    provider: str
    model: str
    connection_id: str
    label: str
    binding_id: str


def _adapter_for(binding: Any, registry: AdapterRegistry) -> Any | None:
    if binding.backend is BindingKind.DIRECT_API:
        from saberops.workers.direct_api import DirectAPIAdapter

        return DirectAPIAdapter(binding.provider)
    return registry.get(binding.provider)


def _resolves(owner: OwnerBindings, binding: Any, registry: AdapterRegistry) -> bool:
    adapter = _adapter_for(binding, registry)
    if adapter is None:
        return False
    try:
        resolve_execution_binding(
            registry=owner.registry, binding_id=binding.binding_id, adapter=adapter
        )
    except BindingResolutionError:
        return False
    return True


def _routable_entries(catalog: ModelCatalog) -> list[Any]:
    return [
        entry
        for entry in catalog.list_models()
        if entry.is_discovery_verified and entry.is_execution_supported and not entry.is_stale
    ]


def _exact_binding(owner: OwnerBindings, role: BindingRole, connection_id: str, model: str) -> Any:
    binding = owner.registry.try_get(
        f"discovered-binding:{role.value.lower()}:{connection_id}:{model}"
    )
    if binding is None or binding.binding_role is not role:
        return None
    return binding


def chain_options(
    owner: OwnerBindings, catalog: ModelCatalog, registry: AdapterRegistry
) -> list[RoutingOption]:
    """Models that may enter a WORKER/SURGEON/INTEGRATOR chain, with evidence."""
    options: dict[str, RoutingOption] = {}
    for entry in _routable_entries(catalog):
        binding = _exact_binding(owner, BindingRole.WORKER, entry.connection_id, entry.model_id)
        if binding is None or not _resolves(owner, binding, registry):
            continue
        try:
            candidate_id = format_candidate_id(entry.provider, entry.model_id)
        except RoutingConfigError:
            continue
        options.setdefault(
            candidate_id,
            RoutingOption(
                key=candidate_id,
                provider=entry.provider,
                model=entry.model_id,
                connection_id=entry.connection_id,
                label=entry.display_name or entry.model_id,
                binding_id=binding.binding_id,
            ),
        )
    return sorted(options.values(), key=lambda option: (option.provider, option.label.lower()))


def reviewer_options(owner: OwnerBindings, registry: AdapterRegistry) -> list[RoutingOption]:
    """REVIEWER bindings that resolve through the real adapter."""
    options: list[RoutingOption] = []
    for binding in owner.registry.list_bindings():
        if binding.binding_role is not BindingRole.REVIEWER:
            continue
        probe = replace(owner, reviewer_binding_id=binding.binding_id, reviewer_binding_ids=None)
        selection = select_reviewer_binding(probe, adapter_registry=registry)
        if selection.binding is None or selection.binding.binding_id != binding.binding_id:
            continue
        if selection.status is not ReviewerSelectionStatus.RESOLVED:
            continue
        parts = binding.binding_id.split(":")
        connection_id = parts[2] if len(parts) >= 4 else ""
        options.append(
            RoutingOption(
                key=binding.binding_id,
                provider=binding.provider,
                model=binding.model,
                connection_id=connection_id,
                label=binding.model,
                binding_id=binding.binding_id,
            )
        )
    return sorted(options, key=lambda option: (option.provider, option.label.lower()))


def _require(job_slug: str) -> Job:
    job = job_by_slug(job_slug)
    if job is None or job.store == "agent":
        raise RoutingEditError("Choose a job that can be changed here.")
    return job


def _approval(option: RoutingOption, catalog: ModelCatalog) -> DynamicCandidate:
    entry = next(
        (
            model
            for model in _routable_entries(catalog)
            if model.connection_id == option.connection_id and model.model_id == option.model
        ),
        None,
    )
    if entry is None:
        raise RoutingEditError(
            "This model has no fresh discovery evidence. Refresh models in Setup."
        )
    if option.key in ALLOWED_CANDIDATES:
        training = "TRUE" if requires_training_permission(option.key) else "FALSE"
    else:
        # No provider reports a training classification for a discovered
        # model, so it stays UNKNOWN and fails closed where training is denied.
        training = "UNKNOWN"
    return DynamicCandidate(
        candidate_id=option.key,
        provider=entry.provider,
        model=entry.model_id,
        connection_id=entry.connection_id,
        backend=entry.backend,
        binding_id=option.binding_id,
        discovery_status=entry.discovery_status.value,
        execution_support=entry.execution_support.value,
        training_required=training,
        capabilities=tuple((name, state.value) for name, state in entry.capabilities),
    )


def add_to_job(
    job_slug: str,
    key: str,
    *,
    owner: OwnerBindings,
    catalog: ModelCatalog,
    registry: AdapterRegistry,
) -> RoutingOption:
    """Append one evidenced model to the end of a job's fallback order."""
    job = _require(job_slug)
    wanted = key.strip()
    if job.store == "reviewer":
        option = next((o for o in reviewer_options(owner, registry) if o.key == wanted), None)
        if option is None:
            raise RoutingEditError(
                "This model cannot check work: it has no usable reviewer binding."
            )
        with _mutation_lock:
            order = owner.role_binding_ids("reviewer")
            if wanted not in order:
                save_owner_bindings(owner.with_role_order("reviewer", (*order, wanted)))
        return option
    option = next((o for o in chain_options(owner, catalog, registry) if o.key == wanted), None)
    if option is None:
        raise RoutingEditError(
            "This model cannot be used here: it has no verified, runnable binding."
        )
    approval = _approval(option, catalog)
    with _mutation_lock:
        payload = load_effective_routing_payload()
        approvals = dict(payload.get(_APPROVED_KEY) or {})
        approvals.setdefault(option.key, approval.to_dict())
        payload[_APPROVED_KEY] = approvals
        try:
            updated = add_candidate_to_chain(payload, role=job.role, candidate_id=option.key)
            save_user_routing(updated)
        except RoutingConfigError as exc:
            raise RoutingEditError(f"Routing not saved: {exc}") from exc
    return option


def remove_from_job(job_slug: str, key: str, *, owner: OwnerBindings) -> None:
    """Remove one model from a job; an emptied job stays empty (no default)."""
    job = _require(job_slug)
    wanted = key.strip()
    with _mutation_lock:
        if job.store == "reviewer":
            order = owner.role_binding_ids("reviewer")
            save_owner_bindings(
                owner.with_role_order("reviewer", tuple(b for b in order if b != wanted))
            )
            return
        try:
            payload = load_effective_routing_payload()
            save_user_routing(
                remove_candidate_from_chain(payload, role=job.role, candidate_id=wanted)
            )
        except RoutingConfigError as exc:
            raise RoutingEditError(f"Routing not saved: {exc}") from exc


def move_in_job(job_slug: str, position: int, direction: str, *, owner: OwnerBindings) -> None:
    """Move the model at 1-based ``position`` one place up or down."""
    job = _require(job_slug)
    step = {"up": -1, "down": 1}.get(direction.strip().lower())
    if step is None:
        raise RoutingEditError("Move up or down.")
    with _mutation_lock:
        if job.store == "reviewer":
            order = list(owner.role_binding_ids("reviewer"))
            index, target = position - 1, position - 1 + step
            if not 0 <= index < len(order):
                raise RoutingEditError("That position is not in the list.")
            if 0 <= target < len(order):
                order[index], order[target] = order[target], order[index]
                save_owner_bindings(owner.with_role_order("reviewer", order))
            return
        try:
            payload = load_effective_routing_payload()
            save_user_routing(
                move_group_in_chain(
                    payload, role=job.role, group=position, direction=direction.strip().lower()
                )
            )
        except RoutingConfigError as exc:
            raise RoutingEditError(f"Routing not saved: {exc}") from exc
