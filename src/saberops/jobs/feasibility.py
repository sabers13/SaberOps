"""Submit-time feasibility (M2-3 section E, pure).

Called once, by ``submit_job``, before the insert. Never called at
dispatch (that is M2-4b's ``admit()``), never contacts a provider,
never reads quota or posture.

Which chain: the candidate set is the build route
(``snapshot.chain_for_role(JobRole.BUILD)``); approved dynamic
candidates in that chain count. A requirement only the build route's
``strong_start`` setting satisfies is not feasible at submit: a strong start
is an escalation setting, not an ordinary route.

``model_family_include`` is checked here with the same
:func:`~saberops.capacity.admission.matches_model_family` matcher admission
uses, so a family request no build-route candidate can satisfy is refused at
submit (``NO_ROUTE_FOR_REQUIREMENTS``) instead of being accepted and parking
later. ``model_family_exclude`` stays an admission filter; the matcher's
semantics are unchanged.
"""

from __future__ import annotations

from saberops.capacity.admission import matches_model_family
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
    chain = list(routing.chain_for_role(JobRole.BUILD))
    if not chain:
        return False, NO_ROUTE_FOR_REQUIREMENTS
    need = set(requirements.required_capabilities)
    if requirements.read_only:
        need.add(Capability.READ_ONLY.value)
    include = requirements.model_family_include
    for candidate in chain:
        if include and not any(
            matches_model_family(candidate, family) for family in include
        ):
            continue
        profile = capability_profile_for(provider=candidate.provider)
        have = {c.value for c in profile.capabilities}
        if need <= have:
            return True, None
    return False, NO_ROUTE_FOR_REQUIREMENTS


__all__ = ["feasible", "role_candidates"]
