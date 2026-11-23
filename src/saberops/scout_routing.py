"""Cheap-model default for scout jobs (F27, folded into effort by H8).

A scout job (F19) is read-only and usually a search, grep or audit question, so
the cheapest plan model of an available subscription is the right default.  The
owner's build route remains the only source of routes: this module never adds a
candidate.  It only chooses the order among the candidates the owner already
allows, keeping every non-cheap candidate as a fallback when the cheap one
cannot be admitted.

H8 folded the scout-specific cheap default into the ordinary effort rule:
``effort: low`` on any job prefers each subscription's cheapest plan model, and
a scout with no stated effort is normalised to ``low`` when the job is created.
A heavy job (``high``/``xhigh`` effort, or a size ``L`` investigation) keeps
the ordinary build route.

The scout route is still overridable per project, with the ``scout_route``
setting: ``cheap`` (default), ``build`` (the ordinary build route, no cheap
preference), or one exact ``provider/model`` already present on the build route.

Matching is by model family, not a frozen catalog, so a renamed cheap model
(``gpt-5.6-luna`` today, ``gpt-6-luna`` in a newer catalog) still matches.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from saberops.jobs.models import Effort, JobRequirements
from saberops.models import WorkerCandidate

#: The default scout route: prefer each subscription's cheapest plan model.
SCOUT_ROUTE_CHEAP: str = "cheap"
#: Use the ordinary build route; no cheap preference.
SCOUT_ROUTE_BUILD: str = "build"

_HEAVY_EFFORTS: frozenset[Effort] = frozenset({Effort.high, Effort.xhigh})
_HEAVY_SIZE_HINT: str = "L"

CandidateKey = tuple[str, str]


def is_cheap_scout_model(provider: str, model: str) -> bool:
    """True when ``provider/model`` is the cheapest plan model of its family.

    The three families named by the plan: Claude Haiku (``anthropic``), Codex
    ``gpt-*-luna`` and OpenCode Go ``deepseek-v4*-flash``.  Everything else is
    left to the owner's route order.
    """
    provider_norm = provider.strip().lower()
    model_norm = model.strip().lower()
    base = model_norm.rsplit("/", 1)[-1]
    if provider_norm == "codex":
        return base == "luna" or base.endswith("-luna")
    if provider_norm == "anthropic":
        return base == "haiku" or base.startswith("claude-haiku")
    if provider_norm == "opencode" and model_norm.startswith("opencode-go/"):
        return "deepseek" in base and "flash" in base
    return False


def subscription_key(provider: str, model: str) -> str:
    """Return the subscription a route spends from (``opencode-go`` is one)."""
    provider_norm = provider.strip().lower()
    model_norm = model.strip().lower()
    if provider_norm == "opencode":
        head = model_norm.split("/", 1)[0] if "/" in model_norm else "zen"
        return f"opencode:{head}"
    return provider_norm


def is_heavy_scout(requirements: JobRequirements | None) -> bool:
    """True when the job explicitly asks for more than the cheap default."""
    if requirements is None:
        return False
    if str(getattr(requirements, "size_hint", "") or "") == _HEAVY_SIZE_HINT:
        return True
    effort = getattr(requirements, "effort", None)
    return isinstance(effort, Effort) and effort in _HEAVY_EFFORTS


def cheap_default_applies(
    requirements: JobRequirements | None, *, effort: str | None = None
) -> bool:
    """True when the job states the cheap default, ``effort: low`` (H8).

    The same rule serves every job, not just scouts: a scout with no stated
    effort is normalised to ``low`` when it is created, so its cheapest-model
    default is this predicate.  A direct run has no job record, so its frozen
    ``effort`` string stands in for the requirement.  A heavy job keeps the
    ordinary build route, and a missing effort never opts in.
    """
    if is_heavy_scout(requirements):
        return False
    if requirements is not None:
        return requirements.effort is Effort.low
    return str(effort or "").strip().lower() == Effort.low.value


def route_candidate_key(route: str | None) -> CandidateKey | None:
    """Parse an explicit ``provider/model`` scout route, else ``None``.

    ``cheap``, ``build`` and an empty value are policy words, not candidates.
    """
    cleaned = str(route or "").strip()
    lowered = cleaned.lower()
    if not cleaned or lowered in (SCOUT_ROUTE_CHEAP, SCOUT_ROUTE_BUILD):
        return None
    provider, separator, model = cleaned.partition("/")
    provider = provider.strip()
    model = model.strip()
    if not separator or not provider or not model or "/" in provider:
        return None
    return (provider, model)


def validate_scout_route(route: str) -> str:
    """Validate one project ``scout_route`` value and return it canonicalized.

    Accepted: ``cheap``, ``build`` (case-insensitive) or an exact
    ``provider/model``.  An exact model is accepted only in canonical form;
    membership in the build route is checked at dispatch, so a setting can be
    saved before the owner adds that route.
    """
    cleaned = str(route or "").strip()
    lowered = cleaned.lower()
    if lowered in (SCOUT_ROUTE_CHEAP, SCOUT_ROUTE_BUILD):
        return lowered
    key = route_candidate_key(cleaned)
    if key is None:
        raise ValueError(
            "scout route must be 'cheap', 'build' or an exact provider/model id"
        )
    provider, model = key
    return f"{provider}/{model}"


def cheap_candidate_keys(
    groups: Sequence[Sequence[WorkerCandidate]],
) -> frozenset[CandidateKey]:
    """The cheap candidates present anywhere in ``groups``."""
    keys: set[CandidateKey] = set()
    for group in groups:
        for candidate in group:
            if is_cheap_scout_model(candidate.provider, candidate.model):
                keys.add((candidate.provider, candidate.model))
    return frozenset(keys)


@dataclass(frozen=True)
class ScoutCandidateView:
    """The groups and pacing a scout job should evaluate, in order."""

    groups: tuple[tuple[WorkerCandidate, ...], ...]
    pacing: tuple[int, ...]
    #: Cheap candidates to prefer after admission, or ``None`` when no cheap
    #: preference applies (build route, heavy scout, or explicit candidate).
    preferred: frozenset[CandidateKey] | None = None


def _pacing_at(pacing: Sequence[int], index: int) -> int:
    return int(pacing[index]) if index < len(pacing) else 0


def _stable_partition(
    candidates: Sequence[WorkerCandidate],
    preferred: frozenset[CandidateKey],
) -> tuple[WorkerCandidate, ...]:
    first = [c for c in candidates if (c.provider, c.model) in preferred]
    rest = [c for c in candidates if (c.provider, c.model) not in preferred]
    return (*first, *rest)


def scout_candidate_view(
    groups: Sequence[Sequence[WorkerCandidate]],
    pacing: Sequence[int],
    *,
    route: str | None,
    requirements: JobRequirements | None,
) -> ScoutCandidateView:
    """Order the owner's build-route candidates for one ``effort: low`` job.

    The owner's groups stay the fallback authority: a group with a cheap
    candidate is tried before a group with none, and every candidate remains
    available.  An explicit ``provider/model`` route narrows the view to that
    candidate (falling back to the cheap default if it is no longer routed).
    A job that does not state ``effort: low``, or a heavy one, keeps the
    ordinary build route (H8).
    """
    original = tuple(tuple(group) for group in groups)
    original_pacing = tuple(int(value) for value in pacing)
    if is_heavy_scout(requirements):
        return ScoutCandidateView(original, original_pacing)
    cleaned_route = str(route or SCOUT_ROUTE_CHEAP).strip()
    if cleaned_route.lower() == SCOUT_ROUTE_BUILD:
        return ScoutCandidateView(original, original_pacing)

    explicit = route_candidate_key(cleaned_route)
    if explicit is not None:
        narrowed: list[tuple[WorkerCandidate, ...]] = []
        narrowed_pacing: list[int] = []
        for index, group in enumerate(original):
            matching = tuple(
                candidate
                for candidate in group
                if (candidate.provider, candidate.model) == explicit
            )
            if matching:
                narrowed.append(matching)
                narrowed_pacing.append(_pacing_at(original_pacing, index))
        if narrowed:
            return ScoutCandidateView(tuple(narrowed), tuple(narrowed_pacing))

    if not cheap_default_applies(requirements):
        return ScoutCandidateView(original, original_pacing)
    cheap = cheap_candidate_keys(original)
    if not cheap:
        return ScoutCandidateView(original, original_pacing)

    order = sorted(
        range(len(original)),
        key=lambda index: (
            0
            if any((c.provider, c.model) in cheap for c in original[index])
            else 1,
            index,
        ),
    )
    reordered = tuple(_stable_partition(original[index], cheap) for index in order)
    reordered_pacing = tuple(_pacing_at(original_pacing, index) for index in order)
    return ScoutCandidateView(reordered, reordered_pacing, preferred=cheap)


def order_candidates_for_scout(
    candidates: Sequence[WorkerCandidate],
    *,
    route: str | None,
    requirements: JobRequirements | None = None,
    effort: str | None = None,
) -> list[WorkerCandidate]:
    """Reorder a flattened chain for a direct ``effort: low`` run (cheap first).

    Every candidate is retained: a cheap model that cannot be admitted falls
    through to the next one.  ``effort`` carries the frozen run config's value
    for a direct run that has no job requirements record; a run whose effort is
    not ``low`` keeps the ordinary build route (H8).
    """
    if is_heavy_scout(requirements):
        return list(candidates)
    cleaned_route = str(route or SCOUT_ROUTE_CHEAP).strip()
    if cleaned_route.lower() == SCOUT_ROUTE_BUILD:
        return list(candidates)

    explicit = route_candidate_key(cleaned_route)
    if explicit is not None and any(
        (c.provider, c.model) == explicit for c in candidates
    ):
        preferred = frozenset({explicit})
    elif cheap_default_applies(requirements, effort=effort):
        preferred = cheap_candidate_keys((tuple(candidates),))
    else:
        return list(candidates)
    if not preferred:
        return list(candidates)
    return list(_stable_partition(candidates, preferred))


def load_scout_route(repo_path: Any) -> str:
    """Read the project's scout route, defaulting to ``cheap``.

    A corrupt project-settings document is left for the existing run-creation
    authority to report; dispatch keeps the documented default instead of
    inventing a route.
    """
    from saberops.project_settings import (
        ProjectSettingsError,
        load_project_settings,
    )

    try:
        settings = load_project_settings(repo_path)
    except (ProjectSettingsError, OSError, ValueError):
        return SCOUT_ROUTE_CHEAP
    return str(settings.scout_route or SCOUT_ROUTE_CHEAP)


__all__ = [
    "CandidateKey",
    "SCOUT_ROUTE_BUILD",
    "SCOUT_ROUTE_CHEAP",
    "ScoutCandidateView",
    "cheap_candidate_keys",
    "cheap_default_applies",
    "is_cheap_scout_model",
    "is_heavy_scout",
    "load_scout_route",
    "order_candidates_for_scout",
    "route_candidate_key",
    "scout_candidate_view",
    "subscription_key",
    "validate_scout_route",
]
