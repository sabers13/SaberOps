"""Deterministic, fail-closed wave admission (ADR-0004 §3 D2, §4 invariant 2).

Covers: ADR-0004 D2 "Admission of a wave is deterministic and fail-closed: lane
file sets disjoint (path-prefix overlap check), base is an ancestor of the
target branch, every route has valid posture."

:func:`admit_wave` is a pure decision over an immutable :class:`~saberops.wave_models.Wave`
plus two injected, explicitly-typed predicates -- the ancestry check (a Git
question) and the route-posture check (an owner-state question). It performs no
I/O of its own when both are supplied, so it is fully hermetic in tests; when no
ancestry checker is injected it falls back to real ``git merge-base`` in the
supplied repository, and when neither a checker nor a repository is available it
fails closed rather than assuming ancestry.

Reasons are stable, ordered strings of the form ``code`` or ``code:detail`` so a
caller can log, route or test them without parsing prose.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from saberops.gate_scope import GateScopeError, normalize_path
from saberops.wave_models import Lane, Wave, WaveModelError

__all__ = [
    "ANCESTRY_UNVERIFIED",
    "BASE_NOT_ANCESTOR",
    "INVALID_TARGET_HEAD",
    "INVALID_WAVE",
    "INVALID_WORKER_ROUTE",
    "INVALID_REVIEWER_ROUTE",
    "LANE_SELF_OVERLAP",
    "OVERLAPPING_TOUCHES",
    "AncestryChecker",
    "GitAncestryChecker",
    "RoutePostureValidator",
    "WaveAdmissionDecision",
    "admit_wave",
    "lane_touch_overlaps",
    "scope_covers",
    "scopes_overlap",
]

#: A lane's Touches overlap another lane's Touches (path-prefix overlap).
OVERLAPPING_TOUCHES = "overlapping_touches"
#: A lane's own Touches overlap its own must-not-touch list.
LANE_SELF_OVERLAP = "lane_self_overlap"
#: The wave description itself is structurally invalid.
INVALID_WAVE = "invalid_wave"
#: ``target_head`` is not a full lowercase hex commit SHA.
INVALID_TARGET_HEAD = "invalid_target_head"
#: ``base_sha`` is not an ancestor of the target head.
BASE_NOT_ANCESTOR = "base_not_ancestor"
#: No ancestry checker and no repository were provided, so ancestry is unproven.
ANCESTRY_UNVERIFIED = "ancestry_unverified"
#: A lane's assigned worker route has no verified valid posture.
INVALID_WORKER_ROUTE = "invalid_worker_route"
#: A lane's assigned reviewer route has no verified valid posture.
INVALID_REVIEWER_ROUTE = "invalid_reviewer_route"

_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

ROLE_WORKER = "worker"
ROLE_REVIEWER = "reviewer"


#: Owner-state predicate: is this role's route in valid posture right now?
RoutePostureValidator = Callable[[str, str], bool]

#: Git predicate: is ``ancestor_sha`` an ancestor of ``descendant_sha``?
AncestryChecker = Callable[[str, str], bool]


class GitAncestryChecker:
    """Default ancestry checker backed by real ``git merge-base --is-ancestor``."""

    def __init__(self, repo: Path | str) -> None:
        self._repo = str(repo)

    def __call__(self, ancestor_sha: str, descendant_sha: str) -> bool:
        from saberops.git import GitManager

        return GitManager.is_ancestor(self._repo, ancestor_sha, descendant_sha)


@dataclass(frozen=True)
class WaveAdmissionDecision:
    """Typed result of :func:`admit_wave`.

    ``reasons`` is empty exactly when ``admitted`` is true. When it is false the
    reasons are stable, ordered codes naming every independent failure found --
    a later check is still evaluated even when an earlier one already failed, so
    one admission attempt reports all of its problems.
    """

    admitted: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        """The first (highest-precedence) reason code, or ``""`` when admitted."""
        return self.reasons[0] if self.reasons else ""


def scope_covers(scope: str, path: str) -> bool:
    """True when canonical ``path`` is ``scope`` or lives under the ``scope`` dir."""
    root = normalize_path(scope).rstrip("/")
    candidate = normalize_path(path).rstrip("/")
    return candidate == root or candidate.startswith(root + "/")


def scopes_overlap(left: Sequence[str], right: Sequence[str]) -> bool:
    """True when any pattern in ``left`` path-prefix overlaps any in ``right``."""
    normalized_left = _normalized_scopes(left)
    normalized_right = _normalized_scopes(right)
    return any(
        scope_covers(a, b) or scope_covers(b, a)
        for a in normalized_left
        for b in normalized_right
    )


def lane_touch_overlaps(
    left: Lane, right: Lane
) -> tuple[tuple[str, str], ...]:
    """Return the ordered ``(left_pattern, right_pattern)`` pairs that overlap."""
    pairs: list[tuple[str, str]] = []
    for a in _normalized_scopes(left.touches):
        for b in _normalized_scopes(right.touches):
            if scope_covers(a, b) or scope_covers(b, a):
                pairs.append((a, b))
    return tuple(pairs)


def _normalized_scopes(patterns: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for pattern in patterns:
        try:
            result.append(normalize_path(pattern))
        except GateScopeError:
            # A malformed pattern cannot be reasoned about; keep it verbatim so
            # the overlap check still sees a distinct token. Structural
            # validation of the wave rejects it separately.
            result.append(str(pattern))
    return tuple(result)


def admit_wave(
    wave: Wave,
    target_head: str,
    posture_validator: RoutePostureValidator,
    *,
    repo: Path | str | None = None,
    ancestry_checker: AncestryChecker | None = None,
) -> WaveAdmissionDecision:
    """Return the deterministic fail-closed admission decision for one wave.

    Order of evaluation (each independent check always runs):

    1. the wave description itself (structural validity);
    2. ``target_head`` shape;
    3. pairwise lane Touches overlap;
    4. each lane's Touches vs its own must-not-touch;
    5. worker and reviewer route posture for every lane;
    6. ancestry of ``wave.base_sha`` in ``target_head``.

    Any failure leaves ``admitted`` false; nothing is spawned and no ref moves.
    """
    reasons: list[str] = []

    reasons.extend(f"{INVALID_WAVE}:{problem}" for problem in wave.problems())

    if not isinstance(target_head, str) or not _SHA_RE.fullmatch(target_head):
        reasons.append(INVALID_TARGET_HEAD)

    lanes = list(wave.lanes)
    for index, left in enumerate(lanes):
        for right in lanes[index + 1 :]:
            for a, b in lane_touch_overlaps(left, right):
                reasons.append(f"{OVERLAPPING_TOUCHES}:{left.lane_id}:{right.lane_id}:{a}:{b}")

    for lane in lanes:
        if scopes_overlap(lane.touches, lane.must_not_touch):
            reasons.append(f"{LANE_SELF_OVERLAP}:{lane.lane_id}")

    for lane in lanes:
        for role, route, code in (
            (ROLE_WORKER, lane.worker_route, INVALID_WORKER_ROUTE),
            (ROLE_REVIEWER, lane.reviewer_route, INVALID_REVIEWER_ROUTE),
        ):
            if not isinstance(route, str) or not route.strip():
                reasons.append(f"{code}:{lane.lane_id}:empty")
                continue
            if not posture_validator(role, route):
                reasons.append(f"{code}:{lane.lane_id}:{route}")

    reasons.extend(_ancestry_reasons(wave, target_head, repo, ancestry_checker))

    return WaveAdmissionDecision(admitted=not reasons, reasons=reasons)


def _ancestry_reasons(
    wave: Wave,
    target_head: str,
    repo: Path | str | None,
    ancestry_checker: AncestryChecker | None,
) -> list[str]:
    checker = ancestry_checker
    if checker is None and repo is not None:
        checker = GitAncestryChecker(repo)
    if checker is None:
        if not _is_sha(wave.base_sha) or not _is_sha(target_head):
            # The shape check already reported INVALID_TARGET_HEAD / a wave
            # problem; do not also claim an unverified ancestry for a token no
            # Git predicate could ever answer.
            return []
        return [f"{ANCESTRY_UNVERIFIED}:{wave.base_sha}:{target_head}"]
    try:
        ancestor = checker(wave.base_sha, target_head)
    except WaveModelError:
        raise
    except Exception as exc:  # noqa: BLE001 - a broken checker must fail closed
        return [f"{ANCESTRY_UNVERIFIED}:{wave.base_sha}:{target_head}:{type(exc).__name__}"]
    if ancestor:
        return []
    return [f"{BASE_NOT_ANCESTOR}:{wave.base_sha}:{target_head}"]


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))
