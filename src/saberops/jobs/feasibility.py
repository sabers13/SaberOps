"""Submit-time feasibility (M2-3 section E, pure).

Called once, by ``submit_job``, before the insert. Never called at
dispatch (that is M2-4b's ``admit()``), never contacts a provider,
never reads quota or posture.

Which chain: the candidate set is the WORKER role chain
(``snapshot.chain_for_role(JobRole.WORKER)``); approved dynamic
candidates in that chain count. This is followup-4's decision: a
requirement only a SURGEON-chain route satisfies is not feasible at
submit.

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
    JobRole,
)
from saberops.routing_config import RoutingSnapshot


def role_candidates(
    snapshot: RoutingSnapshot, role: JobRole | str
) -> list[tuple[str, str]]:
    """Return the deduplicated (provider, model) chain for ``role``."""
    seen: set[tuple[str, str]] = set()
    ordered: list[tuple[str, str]] = []
    for candidate in snapshot.chain_for_role(role):
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
    candidates = role_candidates(routing, JobRole.WORKER)
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


__all__ = ["feasible", "role_candidates"]
