"""Submit-time feasibility (M2-3 section E, pure).

Called once, by ``submit_job``, before the insert. Never called at
dispatch (that is M2-4b's ``admit()``), never contacts a provider,
never reads quota or posture.

Which chain keys: the candidate set for WORKER is the stable union of
the automatic worker chains ``T1.default``, ``T2.default`` and
``T3.default`` (schema 2; legacy schema 1 documents merge into the
same three defaults via ``canonical_routing_payload``). This is today's
automatic tier routing; R-002 replaces it with the Worker role chain.
Implemented as the union of every chain field on the snapshot, so both
schema versions are covered without branching.

``model_family_include`` / ``model_family_exclude`` are validated and
stored only (non-empty lowercase strings, disjoint). They are not part
of feasibility in this lane; family enforcement is M2-4b admission.
"""

from __future__ import annotations

from saberops.control_plane.capabilities import Capability, capability_profile_for
from saberops.jobs.models import (
    DISPATCHABLE_ROLES,
    NO_ROUTE_FOR_REQUIREMENTS,
    JobRequirements,
)
from saberops.routing_config import RoutingSnapshot


def worker_candidates(snapshot: RoutingSnapshot) -> list[tuple[str, str]]:
    """Return the deduplicated (provider, model) union of automatic chains."""
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    chains = (
        snapshot.t1_default,
        snapshot.t2_training_allowed,
        snapshot.t2_training_denied_off_peak,
        snapshot.t2_training_denied_peak,
        snapshot.t3_off_peak,
        snapshot.t3_peak,
    )
    for chain in chains:
        for candidate in chain:
            key = (candidate.provider, candidate.model)
            if key not in seen:
                seen.add(key)
                ordered.append(key)
    return ordered


def feasible(
    requirements: JobRequirements, routing: RoutingSnapshot
) -> tuple[bool, str | None]:
    """Return (True, None) when a route could ever satisfy ``requirements``."""
    if requirements.role not in DISPATCHABLE_ROLES:
        return False, NO_ROUTE_FOR_REQUIREMENTS
    candidates = worker_candidates(routing)
    if not candidates:
        return False, NO_ROUTE_FOR_REQUIREMENTS
    need = set(requirements.required_capabilities)
    if requirements.read_only:
        need.add(Capability.READ_ONLY.value)
    for provider, _model in candidates:
        profile = capability_profile_for(provider=provider)
        have = {c.value for c in profile.capabilities}
        if need <= have:
            return True, None
    return False, NO_ROUTE_FOR_REQUIREMENTS


__all__ = ["feasible", "worker_candidates"]
