"""Roadmap change requests: intake, triage, impact check and revisions (Step 025 / RC-1).

What this module is
-------------------
The deterministic half of ``ROADMAP/CHANGE-WORKFLOW.md``: the part the finished
SaberOps engine must do with software instead of an Orchestrator's memory. It is
a pure planning aid plus two thin, lock-guarded filesystem operations:

* **Idea intake** -- :class:`RoadmapIdea` models one captured idea with the
  owner's exact words; :func:`capture_idea` writes the same
  ``ROADMAP/ideas/I-<timestamp>-<slug>.md`` file the manual ``orch idea``
  command writes, and :func:`read_idea` reads it back without losing a character
  of those words.
* **Triage (stage 1, routes A-E)** -- :func:`triage_idea` validates one idea's
  declared route against live plan state, lanes and files, and returns a
  :class:`TriageDecision`. Routes are A (expand an open step), B (new step that
  consumes a sealed step through an INTEGRATE dependency), C (new independent
  step), D (process only) and E1..E4 (already covered / conflicts with in-flight
  work / rejected / later).
* **Impact check (stage 2)** -- :func:`impact_check` produces the impact table
  rows of CHANGE-WORKFLOW section 2: steps touched and their state, the
  frozen-step guard, file-scope overlap with in-flight lanes, dependency
  changes, declared owner/ADR conflicts, the renumber preview (``shifted:`` /
  ``added:``), cost and makespan.
* **Frozen-step guard** -- :func:`check_plan_change` and
  :func:`assert_frozen_guard` re-implement ``orch roadmap check --prev`` in
  typed software: a sealed (``frozen`` or ``done``) step's title, phase,
  outcome, exit, dependencies, optionality, state and number are refused
  changes, removal of a sealed step is refused, and removing an open step needs
  a reason in ``removed``.
* **Renumber mapping (stage 3)** -- :func:`choose_placement` picks the default
  placement that shifts only open steps, :func:`insert_step` renumbers
  contiguously and records ``renumbered[<stable id>]`` justifications (R1/R2/R3)
  for every sealed step that had to move, and :class:`RenumberPreview` carries
  the full old-number -> new-number mapping.
* **Revision proposal (stages 4-6)** -- :func:`build_proposal` turns a batch of
  triaged ideas into one :class:`RevisionProposal` (proposal markdown plus a new
  in-memory plan), and :func:`apply_revision` converts an owner-approved
  proposal into one immutable versioned
  :class:`~saberops.project_scope.plan_store.PlanRevision` in PL-1's
  :class:`~saberops.project_scope.plan_store.PlanStore`, under an exclusive
  ``.revision.lock``.

Guarantees encoded here

1. A sealed step and its dependencies never change through a roadmap change.
2. Running lanes are never touched: :func:`running_lane_conflicts` refuses a
   revision that changes or removes a step which has an in-flight lane, and this
   module writes nothing except the idea file, the plan-store revision and the
   lock file. It never edits a lane prompt, worktree, branch or gate.
3. A revision launches nothing: it only produces a new version of the plan.
4. One revision at a time (:class:`RevisionLock`).
5. Owner words survive verbatim.
6. The Orchestrator proposes; only an owner decision authorises
   :func:`apply_revision`.

Integration is read-only: :func:`to_plan_task_defs` adapts a plan to PL-1's
:class:`~saberops.project_scope.plan_store.PlanTaskDef` rows and
:func:`roadmap_graph` / :func:`roadmap_makespan` reuse WP-1's
:mod:`saberops.wave_plan_optimizer` for the makespan estimate. Neither authority
is modified.

Zero model, provider or network calls. The only clock reads are the explicit
capture/apply defaults (pass ``captured_at`` / ``created_at`` for determinism);
nothing else is time-dependent.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath

from saberops.project_scope.plan_store import (
    PlanRevision,
    PlanStore,
    PlanTaskDef,
)
from saberops.ready_frontier import TaskRef
from saberops.wave_plan_optimizer import (
    OptimizationLimits,
    PlanNode,
    TaskGraph,
    optimize_plan,
)

__all__ = [
    "COST_SESSIONS_PER_EXPANSION",
    "COST_SESSIONS_PER_NEW_STEP",
    "DEFAULT_ROADMAP_PROJECT_ID",
    "DEFAULT_STEP_SIZE",
    "DEFAULT_TERMINAL_STEP_ID",
    "GUARDED_STEP_FIELDS",
    "IDEAS_DIRNAME",
    "IMPACT_ROWS",
    "MAX_STEPS",
    "PROPOSALS_DIRNAME",
    "RENUMBER_REASON_LEGEND",
    "RENUMBER_REASONS",
    "REVISION_LOCK_FILENAME",
    "TRIAGE_FAMILIES",
    "TRIAGE_ROUTES",
    "AppliedRevision",
    "CostImpact",
    "FrozenStepError",
    "FrozenViolation",
    "IdeaFormatError",
    "IdeaState",
    "ImpactReport",
    "InFlightLane",
    "MakespanImpact",
    "NewStepSpec",
    "NumberShift",
    "PlacementDecision",
    "PlanChangeCheck",
    "RenumberPreview",
    "RevisionLock",
    "RevisionLockError",
    "RevisionNotApprovedError",
    "RevisionProposal",
    "RoadmapChangeError",
    "RoadmapIdea",
    "RoadmapPlan",
    "RoadmapStep",
    "RunningLaneError",
    "ScopeCollision",
    "StepPhase",
    "StepState",
    "TriageContext",
    "TriageDecision",
    "TriageError",
    "TriageRoute",
    "ViolationKind",
    "apply_revision",
    "assert_frozen_guard",
    "build_proposal",
    "capture_idea",
    "check_plan_change",
    "choose_placement",
    "default_revision_lock_path",
    "detect_collisions",
    "dump_plan",
    "evaluate_placement",
    "expand_step",
    "idea_path",
    "impact_check",
    "insert_step",
    "load_ideas",
    "load_plan",
    "read_idea",
    "remove_step",
    "renumber_preview",
    "roadmap_graph",
    "roadmap_makespan",
    "running_lane_conflicts",
    "scopes_overlap",
    "steps_touched",
    "to_plan_task_defs",
    "triage_batch",
    "triage_idea",
    "write_idea",
]

#: Project identity used when a roadmap is adapted into the plan store.
DEFAULT_ROADMAP_PROJECT_ID = "saberops-completion"

#: Stable ID of the terminal certification gate (never the last *place* to insert).
DEFAULT_TERMINAL_STEP_ID = "END"

#: Directory name of the captured ideas under ``ROADMAP/``.
IDEAS_DIRNAME = "ideas"

#: Directory name of the proposals under ``ROADMAP/``.
PROPOSALS_DIRNAME = "proposals"

#: Name of the single-revision lock file (CHANGE-WORKFLOW 1.4).
REVISION_LOCK_FILENAME = ".revision.lock"

#: WORKFLOW section 15 format limit: at most 999 display numbers.
MAX_STEPS = 999

#: Default S/M/L planning size of a step without an explicit one.
DEFAULT_STEP_SIZE = "M"

#: The five route families named by CHANGE-WORKFLOW section 2.
TRIAGE_FAMILIES: tuple[str, ...] = ("A", "B", "C", "D", "E")

#: Row names of the CHANGE-WORKFLOW section 2 impact check, in order.
IMPACT_ROWS: tuple[str, ...] = (
    "Steps touched, and their state",
    "Frozen-step guard",
    "Overlap with in-flight lanes",
    "Dependencies",
    "Conflicts with owner decisions / ADRs",
    "Renumber impact",
    "Cost (sessions, gates, owner decisions)",
    "Makespan impact",
)

#: Justification codes for renumbering a sealed step (CHANGE-WORKFLOW 3.2).
RENUMBER_REASONS: tuple[str, ...] = ("R1", "R2", "R3")

RENUMBER_REASON_LEGEND: Mapping[str, str] = {
    "R1": "reading order: the new step is a prerequisite of a sealed step's integration",
    "R2": "phase coherence: placing it at the end would hide it from its phase",
    "R3": "consolidation: an owner-ordered cleanup after many revisions",
}

#: Planning cost of one new step (heuristic; mirror of CHANGE-WORKFLOW stage 2).
COST_SESSIONS_PER_NEW_STEP: Mapping[str, int] = {
    "worker": 1,
    "reviewer": 1,
    "integrator": 1,
    "gate_runs": 1,
}

#: Planning cost of one expansion of an already-open step.
COST_SESSIONS_PER_EXPANSION: Mapping[str, int] = {
    "worker": 1,
    "reviewer": 1,
    "integrator": 0,
    "gate_runs": 1,
}

_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_IDEA_ID_RE = re.compile(r"^I-\d{8}-\d{6}-(?P<slug>.+)$")
_PROJECT_SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SIZE_VALUES = frozenset(("S", "M", "L"))

_IDEA_SECTION_WORDS = "## Owner words (verbatim)"
_IDEA_SECTION_TRIAGE = "## Triage"


# ---------------------------------------------------------------------------
# Errors and enums
# ---------------------------------------------------------------------------


class RoadmapChangeError(ValueError):
    """Raised for a malformed roadmap, idea, request or impact result."""


class IdeaFormatError(RoadmapChangeError):
    """Raised when an idea file is missing required content or is malformed."""


class TriageError(RoadmapChangeError):
    """Raised when a declared triage route contradicts live plan/lane state."""


class FrozenStepError(RoadmapChangeError):
    """Raised when a change would alter, reopen or remove a sealed step."""


class RevisionLockError(RoadmapChangeError):
    """Raised when a revision is already in progress (``.revision.lock``)."""


class RevisionNotApprovedError(RoadmapChangeError):
    """Raised when an unapproved proposal is applied to the plan store."""


class RunningLaneError(RoadmapChangeError):
    """Raised when a revision would touch a step with an in-flight lane."""


class StepState(StrEnum):
    """Lifecycle state of a roadmap step in ``steps.json``."""

    OPEN = "open"
    FROZEN = "frozen"
    DONE = "done"


class IdeaState(StrEnum):
    """Lifecycle state of a captured idea (the idea file's ``status``)."""

    CAPTURED = "captured"
    TRIAGED = "triaged"
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    LATER = "later"
    APPLIED = "applied"


class TriageRoute(StrEnum):
    """Triage route of one idea (CHANGE-WORKFLOW section 2, stage 1).

    ``A``/``B``/``C``/``D`` are the four routes that act on the plan;
    ``E1``..``E4`` are the four "not now" sub-cases (already covered, conflicts
    with in-flight work, rejected, later).
    """

    A = "A"
    B = "B"
    C = "C"
    D = "D"
    E1 = "E1"
    E2 = "E2"
    E3 = "E3"
    E4 = "E4"

    @property
    def family(self) -> str:
        """Route family letter: ``A``-``D`` itself, ``E`` for every E sub-case."""
        return "E" if self.value.startswith("E") else self.value

    @property
    def creates_step(self) -> bool:
        """True when the route adds a new step (routes B and C)."""
        return self in (TriageRoute.B, TriageRoute.C)

    @property
    def changes_plan(self) -> bool:
        """True when the route changes the plan (routes A, B and C)."""
        return self in (TriageRoute.A, TriageRoute.B, TriageRoute.C)

    @classmethod
    def parse(cls, value: object) -> TriageRoute:
        """Parse ``"A"``..``"E4"`` case-insensitively, failing closed otherwise."""
        text = _clean(value).upper()
        try:
            return cls(text)
        except ValueError as exc:
            raise TriageError(
                f"unknown triage route {value!r}: use one of "
                f"{', '.join(route.value for route in TRIAGE_ROUTES)}"
            ) from exc


#: Every triage route, in order; E has four sub-cases.
TRIAGE_ROUTES: tuple[TriageRoute, ...] = (
    TriageRoute.A,
    TriageRoute.B,
    TriageRoute.C,
    TriageRoute.D,
    TriageRoute.E1,
    TriageRoute.E2,
    TriageRoute.E3,
    TriageRoute.E4,
)


class ViolationKind(StrEnum):
    """Why a plan change is refused by the frozen-step guard."""

    INVALID = "invalid"
    CHANGED = "changed"
    REOPENED = "reopened"
    RENUMBERED = "renumbered"
    REMOVED = "removed"


# ---------------------------------------------------------------------------
# Small narrowing helpers (fail closed, never guess)
# ---------------------------------------------------------------------------


def _clean(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _slugify(value: object, *, fallback: str = "idea") -> str:
    slug = _SLUG_RE.sub("-", _clean(value).lower()).strip("-")
    return slug or fallback


def _slug_from_idea_id(idea_id: str) -> str:
    match = _IDEA_ID_RE.match(_clean(idea_id))
    if match is not None:
        return _slugify(match.group("slug"))
    return _slugify(idea_id)


def _single_line(value: object) -> str:
    return " ".join(_clean(value).split())


def _dedup(values: Iterable[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        text = value.strip()
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)


def _as_mapping(value: object, what: str) -> Mapping[object, object]:
    if not isinstance(value, Mapping):
        raise RoadmapChangeError(f"{what} must be a mapping")
    return value


def _require_str(data: Mapping[object, object], key: str, default: str = "") -> str:
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if not isinstance(value, str):
        raise RoadmapChangeError(f"{key} must be a string, got {type(value).__name__}")
    return value


def _require_int(data: Mapping[object, object], key: str, default: int) -> int:
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise RoadmapChangeError(f"{key} must be an integer, got {value!r}")
    return value


def _optional_int(data: Mapping[object, object], key: str) -> int | None:
    if key not in data or data[key] is None:
        return None
    return _require_int(data, key, 0)


def _require_bool(data: Mapping[object, object], key: str, default: bool = False) -> bool:
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if not isinstance(value, bool):
        raise RoadmapChangeError(f"{key} must be a boolean, got {value!r}")
    return value


def _front_bool(front: Mapping[str, str], key: str, default: bool = False) -> bool:
    if key not in front:
        return default
    text = front[key].strip().lower()
    if text in ("true", "1", "yes"):
        return True
    if text in ("false", "0", "no", ""):
        return False
    raise IdeaFormatError(f"{key} must be true or false, got {front[key]!r}")


def _string_tuple(value: object, what: str) -> tuple[str, ...]:
    """Parse a string list that may be a JSON list or a comma-separated line."""
    if value is None:
        return ()
    if isinstance(value, str):
        return _dedup(value.split(","))
    if isinstance(value, (list, tuple)):
        collected: list[str] = []
        for entry in value:
            if not isinstance(entry, str) or not entry.strip():
                raise RoadmapChangeError(f"{what} entries must be non-empty strings")
            collected.append(entry)
        return _dedup(collected)
    raise RoadmapChangeError(f"{what} must be a list of strings or a comma-separated string")


def _pair_tuple(value: object, what: str) -> tuple[tuple[str, str], ...]:
    """Parse ``{id: text}`` (or ``[[id, text], ...]``) into ordered pairs."""
    if value is None:
        return ()
    entries: list[tuple[str, str]] = []
    if isinstance(value, Mapping):
        items: Iterable[object] = list(value.items())
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise RoadmapChangeError(f"{what} must be a mapping of id to text")
    for entry in items:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise RoadmapChangeError(f"{what} entries must be [id, text] pairs")
        key, text = entry
        if not isinstance(key, str) or not key.strip():
            raise RoadmapChangeError(f"{what} keys must be non-empty strings")
        if not isinstance(text, str):
            raise RoadmapChangeError(f"{what} values must be strings")
        entries.append((key.strip(), text.strip()))
    return tuple(entries)


# ---------------------------------------------------------------------------
# Write-scope semantics (same rules as ready_frontier / wave_plan_optimizer)
# ---------------------------------------------------------------------------


def _canonical_scope(raw: object) -> str | None:
    """Canonical repo-relative form of one declared write scope, or ``None``.

    Benign variants (``a/b``, ``a/b/``, ``./a/b``) fold to one form; unsafe
    forms (absolute, traversal, backslash, NUL, embedded glob) return ``None``
    so callers fail closed.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text or "\\" in text or "\x00" in text:
        return None
    subtree = text.endswith("/**")
    literal = text[:-3] if subtree else text
    if not literal or any(char in literal for char in "*?[]"):
        return None
    path = PurePosixPath(literal)
    if path.is_absolute() or ".." in path.parts:
        return None
    canonical = path.as_posix()
    if canonical in ("", "."):
        return None
    return f"{canonical}/**" if subtree else canonical


def _require_scope(raw: str, what: str) -> str:
    canonical = _canonical_scope(raw)
    if canonical is None:
        raise RoadmapChangeError(f"unsafe or ambiguous {what} {raw!r}")
    return canonical


def _base_scope(scope: str) -> str:
    return scope[:-3] if scope.endswith("/**") else scope


def _path_owned(path: str, scope: str) -> bool:
    return path == scope or (scope.endswith("/**") and path.startswith(scope[:-2]))


def _scope_owns(left: str, right: str) -> bool:
    """True when two canonical scopes touch (exact path or ``/**`` subtree)."""
    return _path_owned(_base_scope(left), right) or _path_owned(_base_scope(right), left)


def scopes_overlap(left: Iterable[str], right: Iterable[str]) -> bool:
    """True when two declared write scopes overlap (exact path or ``/**`` subtree).

    Unsafe or ambiguous scopes raise :class:`RoadmapChangeError` instead of
    being silently treated as disjoint.
    """
    left_scopes = tuple(_require_scope(scope, "write scope") for scope in left)
    right_scopes = tuple(_require_scope(scope, "write scope") for scope in right)
    return any(_scope_owns(a, b) for a in left_scopes for b in right_scopes)


# ---------------------------------------------------------------------------
# Plan model (steps.json)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepPhase:
    """One phase row of ``steps.json``."""

    id: str
    title: str = ""

    def as_dict(self) -> dict[str, object]:
        """Serialize for storage."""
        return {"id": self.id, "title": self.title}

    @classmethod
    def from_dict(cls, data: object) -> StepPhase:
        """Reconstruct from a persisted mapping, validating shapes."""
        mapping = _as_mapping(data, "phase")
        return cls(
            id=_clean(_require_str(mapping, "id")),
            title=_require_str(mapping, "title"),
        )


#: Step fields the frozen-step guard refuses to change on a sealed step.
GUARDED_STEP_FIELDS: tuple[str, ...] = (
    "title",
    "phase",
    "outcome",
    "exit_criteria",
    "needs",
    "integration_needs",
    "optional",
)


@dataclass(frozen=True)
class RoadmapStep:
    """One roadmap step: a stable ID plus its display position and state.

    Display numbers (``n``) are presentation only; every reference between steps
    uses the stable ``id``. ``exit_criteria`` is the ``steps.json`` ``exit``
    field, ``integration_needs`` are deferred INTEGRATE edges that never block a
    start, and ``owns`` / ``size`` carry the optional WP-1 planning metadata.
    """

    id: str
    title: str = ""
    phase: str = ""
    n: int = 0
    state: StepState = StepState.OPEN
    needs: tuple[str, ...] = ()
    integration_needs: tuple[str, ...] = ()
    optional: bool = False
    outcome: str = ""
    exit_criteria: str = ""
    frozen_evidence: str = ""
    added: int | None = None
    owns: tuple[str, ...] = ()
    size: str = DEFAULT_STEP_SIZE

    def __post_init__(self) -> None:
        """Canonicalize identity and planning fields, failing closed."""
        object.__setattr__(self, "id", _clean(self.id))
        if not self.id:
            raise RoadmapChangeError("every roadmap step needs a stable non-empty id")
        object.__setattr__(self, "title", _clean(self.title))
        object.__setattr__(self, "phase", _clean(self.phase))
        object.__setattr__(self, "needs", _dedup(self.needs))
        object.__setattr__(self, "integration_needs", _dedup(self.integration_needs))
        object.__setattr__(self, "owns", _dedup(self.owns))
        size = _clean(self.size).upper()
        if size not in _SIZE_VALUES:
            raise RoadmapChangeError(f"step {self.id}: unknown size {self.size!r}: use S, M or L")
        object.__setattr__(self, "size", size)

    @property
    def sealed(self) -> bool:
        """True for a step with evidence (``frozen``) or a completed step (``done``)."""
        return self.state in (StepState.FROZEN, StepState.DONE)

    def field_value(self, name: str) -> object:
        """Return a guarded field's value (used by the frozen-step guard)."""
        return self.exit_criteria if name == "exit" else getattr(self, name)

    def as_dict(self) -> dict[str, object]:
        """Serialize to the ``steps.json`` step shape (extra keys only when set)."""
        record: dict[str, object] = {
            "id": self.id,
            "title": self.title,
            "phase": self.phase,
            "needs": list(self.needs),
            "optional": self.optional,
            "outcome": self.outcome,
            "exit": self.exit_criteria,
            "n": self.n,
            "state": self.state.value,
        }
        if self.integration_needs:
            record["integration_needs"] = list(self.integration_needs)
        if self.frozen_evidence:
            record["frozen_evidence"] = self.frozen_evidence
        if self.added is not None:
            record["added"] = self.added
        if self.owns:
            record["owns"] = list(self.owns)
        if self.size != DEFAULT_STEP_SIZE:
            record["size"] = self.size
        return record

    @classmethod
    def from_dict(cls, data: object) -> RoadmapStep:
        """Reconstruct from a persisted mapping, validating shapes."""
        mapping = _as_mapping(data, "step")
        return cls(
            id=_require_str(mapping, "id"),
            title=_require_str(mapping, "title"),
            phase=_require_str(mapping, "phase"),
            n=_require_int(mapping, "n", 0),
            state=_parse_state(mapping),
            needs=_string_tuple(mapping.get("needs"), "needs"),
            integration_needs=_string_tuple(mapping.get("integration_needs"), "integration_needs"),
            optional=_require_bool(mapping, "optional"),
            outcome=_require_str(mapping, "outcome"),
            exit_criteria=_require_str(mapping, "exit"),
            frozen_evidence=_require_str(mapping, "frozen_evidence"),
            added=_optional_int(mapping, "added"),
            owns=_string_tuple(mapping.get("owns"), "owns"),
            size=_require_str(mapping, "size", DEFAULT_STEP_SIZE),
        )


def _parse_state(mapping: Mapping[object, object]) -> StepState:
    text = _clean(_require_str(mapping, "state", StepState.OPEN.value)).lower()
    try:
        return StepState(text)
    except ValueError as exc:
        raise RoadmapChangeError(f"bad step state {text!r}: use open, frozen or done") from exc


@dataclass(frozen=True)
class RoadmapPlan:
    """One versioned roadmap (``ROADMAP/steps.json``) in typed form.

    ``renumbered`` and ``removed`` hold this revision's justifications and
    removal reasons exactly as the manual validator expects them: a mapping from
    stable step ID to text. Construction does not enforce plan invariants (so a
    deliberately broken plan can be validated); :meth:`validate` does, and
    :meth:`require_valid` raises.
    """

    plan: str = DEFAULT_ROADMAP_PROJECT_ID
    version: int = 1
    date: str = ""
    note: str = ""
    external_prereqs: tuple[str, ...] = ()
    phases: tuple[StepPhase, ...] = ()
    steps: tuple[RoadmapStep, ...] = ()
    renumbered: tuple[tuple[str, str], ...] = ()
    removed: tuple[tuple[str, str], ...] = ()

    @property
    def step_ids(self) -> tuple[str, ...]:
        """Every stable step ID in display order."""
        return tuple(step.id for step in self.steps)

    @property
    def step_count(self) -> int:
        """Number of steps in the plan."""
        return len(self.steps)

    @property
    def next_version(self) -> int:
        """Version this plan's next revision would carry."""
        return self.version + 1

    def phase_ids(self) -> tuple[str, ...]:
        """Every phase ID in plan order."""
        return tuple(phase.id for phase in self.phases)

    def has_step(self, step_id: str) -> bool:
        """True when a step with this stable ID exists."""
        return any(step.id == step_id for step in self.steps)

    def step(self, step_id: str) -> RoadmapStep:
        """Return a step by stable ID, raising :class:`RoadmapChangeError`."""
        for step in self.steps:
            if step.id == step_id:
                return step
        raise RoadmapChangeError(f"unknown roadmap step {step_id!r}")

    def step_at(self, position: int) -> RoadmapStep:
        """Return the step at one 1-based display position."""
        if not 1 <= position <= len(self.steps):
            raise RoadmapChangeError(f"no step at display position {position}")
        return self.steps[position - 1]

    def phase_steps(self, phase_id: str) -> tuple[RoadmapStep, ...]:
        """Every step of one phase, in display order."""
        return tuple(step for step in self.steps if step.phase == phase_id)

    def sealed_steps(self) -> tuple[RoadmapStep, ...]:
        """Every ``frozen`` or ``done`` step."""
        return tuple(step for step in self.steps if step.sealed)

    def sealed_ids(self) -> tuple[str, ...]:
        """Stable IDs of every sealed step."""
        return tuple(step.id for step in self.sealed_steps())

    def open_ids(self) -> tuple[str, ...]:
        """Stable IDs of every ``open`` step."""
        return tuple(step.id for step in self.steps if step.state is StepState.OPEN)

    def terminal_step(self) -> RoadmapStep | None:
        """The terminal certification gate, else the last step."""
        for step in self.steps:
            if step.id == DEFAULT_TERMINAL_STEP_ID:
                return step
        return self.steps[-1] if self.steps else None

    def sealed_steps_at_or_after(self, position: int) -> tuple[RoadmapStep, ...]:
        """Sealed steps whose display number is at or beyond ``position``."""
        return tuple(step for step in self.steps if step.n >= position and step.sealed)

    def renumbered_map(self) -> dict[str, str]:
        """This revision's renumber justifications keyed by stable ID."""
        return dict(self.renumbered)

    def removed_map(self) -> dict[str, str]:
        """This revision's removal reasons keyed by stable ID."""
        return dict(self.removed)

    def validate(self) -> tuple[str, ...]:
        """Return every plan error, mirroring ``orch roadmap check`` with no prev.

        Checks unique/non-empty phase and step IDs, contiguous ``n`` from 1,
        known phases, contiguity of phase groups, known and non-self
        dependencies (including ``external_prereqs``) and dependency cycles. A
        bad step *state* is refused at construction instead (:class:`StepState`).
        """
        errors: list[str] = []
        phase_ids = [phase.id for phase in self.phases]
        if len(set(phase_ids)) != len(phase_ids) or any(not phase for phase in phase_ids):
            errors.append("phase ids must be unique and non-empty")
        ids = [step.id for step in self.steps]
        if len(set(ids)) != len(ids):
            errors.append("step ids must be unique and non-empty")
        known = set(ids) | set(self.external_prereqs)
        for position, step in enumerate(self.steps, 1):
            if step.n != position:
                errors.append(
                    f"{step.id}: n={step.n} but position {position} "
                    "(numbers must be contiguous 1..N)"
                )
            if step.phase not in phase_ids:
                errors.append(f"{step.id}: unknown phase {step.phase!r}")
            for need in (*step.needs, *step.integration_needs):
                if need == step.id:
                    errors.append(f"{step.id}: needs itself")
                elif need not in known:
                    errors.append(f"{step.id}: needs unknown id {need!r}")
        if len(self.steps) > MAX_STEPS:
            errors.append("more than 999 steps")
        order = [phase_ids.index(step.phase) for step in self.steps if step.phase in phase_ids]
        if order != sorted(order):
            errors.append("phases must group steps contiguously in phase order")
        cycle = _dependency_cycle(
            {step.id: tuple(need for need in step.needs if need in known) for step in self.steps}
        )
        if cycle:
            errors.append("dependency cycle: " + " -> ".join(cycle))
        return tuple(errors)

    def require_valid(self) -> None:
        """Raise :class:`RoadmapChangeError` when the plan is invalid."""
        errors = self.validate()
        if errors:
            raise RoadmapChangeError("invalid roadmap plan: " + "; ".join(errors))

    def as_dict(self) -> dict[str, object]:
        """Serialize to the ``steps.json`` document shape."""
        return {
            "plan": self.plan,
            "version": self.version,
            "date": self.date,
            "note": self.note,
            "external_prereqs": list(self.external_prereqs),
            "phases": [phase.as_dict() for phase in self.phases],
            "steps": [step.as_dict() for step in self.steps],
            "renumbered": dict(self.renumbered),
            "removed": dict(self.removed),
        }

    @classmethod
    def from_dict(cls, data: object) -> RoadmapPlan:
        """Reconstruct from a persisted mapping, validating shapes only."""
        mapping = _as_mapping(data, "plan")
        phases = mapping.get("phases", ())
        steps = mapping.get("steps", ())
        if not isinstance(phases, (list, tuple)) or not isinstance(steps, (list, tuple)):
            raise RoadmapChangeError("plan phases and steps must be lists")
        return cls(
            plan=_require_str(mapping, "plan", DEFAULT_ROADMAP_PROJECT_ID),
            version=_require_int(mapping, "version", 1),
            date=_require_str(mapping, "date"),
            note=_require_str(mapping, "note"),
            external_prereqs=_string_tuple(mapping.get("external_prereqs"), "external_prereqs"),
            phases=tuple(StepPhase.from_dict(entry) for entry in phases),
            steps=tuple(RoadmapStep.from_dict(entry) for entry in steps),
            renumbered=_pair_tuple(mapping.get("renumbered"), "renumbered"),
            removed=_pair_tuple(mapping.get("removed"), "removed"),
        )


def _dependency_cycle(graph: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    """Return one dependency cycle as a path, or ``()`` when acyclic."""
    state: dict[str, int] = {}

    def visit(node: str, trail: tuple[str, ...]) -> tuple[str, ...] | None:
        mark = state.get(node)
        if mark == 1:
            return (*trail, node)
        if mark == 2:
            return None
        state[node] = 1
        for nxt in graph.get(node, ()):
            found = visit(nxt, (*trail, node))
            if found is not None:
                return found
        state[node] = 2
        return None

    for node in sorted(graph):
        found = visit(node, ())
        if found is not None:
            return found
    return ()


def load_plan(path: Path | str) -> RoadmapPlan:
    """Load a roadmap plan from a ``steps.json`` file."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RoadmapChangeError(f"{path}: not valid JSON: {exc}") from exc
    return RoadmapPlan.from_dict(data)


def dump_plan(plan: RoadmapPlan) -> str:
    """Render a plan as deterministic ``steps.json`` text."""
    return json.dumps(plan.as_dict(), indent=1, sort_keys=False) + "\n"


# ---------------------------------------------------------------------------
# Frozen-step guard and renumber preview
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NumberShift:
    """One step whose display number moves in a revision."""

    step_id: str
    old: int
    new: int
    state: StepState = StepState.OPEN

    @property
    def sealed(self) -> bool:
        """True when the shifted step was sealed before the revision."""
        return self.state in (StepState.FROZEN, StepState.DONE)

    def describe(self) -> str:
        """``<ID> 00N->00M`` exactly as the manual validator prints it."""
        return f"{self.step_id} {self.old:03d}->{self.new:03d}"


@dataclass(frozen=True)
class RenumberPreview:
    """The minimal renumber impact of a revision (CHANGE-WORKFLOW section 3)."""

    shifted: tuple[NumberShift, ...] = ()
    added: tuple[tuple[str, int], ...] = ()
    removed: tuple[str, ...] = ()

    @property
    def sealed_shifted(self) -> tuple[NumberShift, ...]:
        """Shifts that moved a sealed step and therefore need a justification."""
        return tuple(shift for shift in self.shifted if shift.sealed)

    @property
    def requires_justification(self) -> bool:
        """True when at least one sealed step shifted."""
        return bool(self.sealed_shifted)

    @property
    def mapping(self) -> tuple[tuple[str, int, int], ...]:
        """Full ``(stable id, old number, new number)`` mapping of the revision."""
        return tuple(
            sorted(
                ((shift.step_id, shift.old, shift.new) for shift in self.shifted),
                key=lambda item: (item[1], item[0]),
            )
        )

    def old_to_new(self) -> dict[int, int]:
        """Old display number -> new display number for every shifted step."""
        return {shift.old: shift.new for shift in self.shifted}

    def summary_lines(self) -> tuple[str, ...]:
        """The manual validator's ``shifted:`` / ``added:`` output lines."""
        shifted = ", ".join(shift.describe() for shift in self.shifted) or "none"
        added = ", ".join(f"{step_id} {number:03d}" for step_id, number in self.added) or "none"
        return (f"shifted: {shifted}", f"added: {added}")


@dataclass(frozen=True)
class FrozenViolation:
    """One refused change to a sealed step (or an unjustified open removal)."""

    step_id: str
    kind: ViolationKind
    detail: str
    field: str = ""

    def describe(self) -> str:
        """Readable one-line description."""
        return f"{self.step_id}: {self.detail}"


@dataclass(frozen=True)
class PlanChangeCheck:
    """The full deterministic result of comparing two plan versions."""

    errors: tuple[str, ...] = ()
    violations: tuple[FrozenViolation, ...] = ()
    renumber: RenumberPreview = field(default_factory=RenumberPreview)

    @property
    def ok(self) -> bool:
        """True when the new plan is valid and the frozen-step guard passed."""
        return not self.errors and not self.violations

    def summary_lines(self) -> tuple[str, ...]:
        """``shifted:`` / ``added:`` lines plus a PASS/FAIL summary line."""
        lines = list(self.renumber.summary_lines())
        lines.extend(violation.describe() for violation in self.violations)
        lines.extend(self.errors)
        status = "PASS" if self.ok else "FAIL"
        lines.append(f"{status}: {len(self.errors)} error(s), {len(self.violations)} violation(s)")
        return tuple(lines)


def renumber_preview(prev: RoadmapPlan, new: RoadmapPlan) -> RenumberPreview:
    """Compute how display numbers moved between two plan versions."""
    previous = {step.id: step for step in prev.steps}
    current = {step.id: step for step in new.steps}
    shifted = tuple(
        NumberShift(
            step_id=step.id,
            old=previous[step.id].n,
            new=step.n,
            state=previous[step.id].state,
        )
        for step in new.steps
        if step.id in previous and previous[step.id].n != step.n
    )
    added = tuple((step.id, step.n) for step in new.steps if step.id not in previous)
    removed = tuple(step.id for step in prev.steps if step.id not in current)
    return RenumberPreview(shifted=shifted, added=added, removed=removed)


def check_plan_change(prev: RoadmapPlan, new: RoadmapPlan) -> PlanChangeCheck:
    """Validate a plan revision against the frozen-step guard (fail closed).

    Mirrors ``orch roadmap check --prev``: a sealed step's guarded fields,
    reopening, unjustified renumbering and removal are refused, an open step may
    not be removed without a reason in ``removed``, and the new plan must itself
    be valid (which also refuses a dependency on a removed step).
    """
    errors = new.validate()
    current = {step.id: step for step in new.steps}
    renumbered = new.renumbered_map()
    removed_map = new.removed_map()
    violations: list[FrozenViolation] = []
    for old in prev.steps:
        step_id = old.id
        fresh = current.get(step_id)
        if fresh is None:
            if old.sealed:
                violations.append(
                    FrozenViolation(step_id, ViolationKind.REMOVED, "sealed step removed")
                )
            elif step_id not in removed_map:
                violations.append(
                    FrozenViolation(
                        step_id, ViolationKind.REMOVED, "removed without a reason in 'removed'"
                    )
                )
            continue
        if not old.sealed:
            continue
        for name in GUARDED_STEP_FIELDS:
            if old.field_value(name) != fresh.field_value(name):
                violations.append(
                    FrozenViolation(
                        step_id,
                        ViolationKind.CHANGED,
                        f"sealed step changed field {name!r}",
                        field=name,
                    )
                )
        if fresh.state is StepState.OPEN:
            violations.append(
                FrozenViolation(
                    step_id, ViolationKind.REOPENED, "sealed step cannot return to open"
                )
            )
        if old.n != fresh.n and step_id not in renumbered:
            violations.append(
                FrozenViolation(
                    step_id,
                    ViolationKind.RENUMBERED,
                    f"sealed step renumbered {old.n:03d}->{fresh.n:03d} "
                    "without a justification in 'renumbered'",
                )
            )
    return PlanChangeCheck(
        errors=errors,
        violations=tuple(violations),
        renumber=renumber_preview(prev, new),
    )


def assert_frozen_guard(prev: RoadmapPlan, new: RoadmapPlan) -> PlanChangeCheck:
    """Return the check after raising :class:`FrozenStepError` when it fails."""
    check = check_plan_change(prev, new)
    if not check.ok:
        raise FrozenStepError(f"roadmap change refused: {_check_reasons(check)}")
    return check


def _check_reasons(check: PlanChangeCheck) -> str:
    return "; ".join([violation.describe() for violation in check.violations] + list(check.errors))


# ---------------------------------------------------------------------------
# Placement, insertion, expansion and removal (stages 3 and 6 edits)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlacementDecision:
    """Where a new step goes and which sealed steps that moves."""

    position: int
    phase: str = ""
    after_step: str = ""
    reason: str = ""
    sealed_steps_shifted: tuple[str, ...] = ()

    @property
    def shifts_sealed(self) -> bool:
        """True when this placement moves at least one sealed step."""
        return bool(self.sealed_steps_shifted)

    @property
    def needs_justification(self) -> bool:
        """True when an R1/R2/R3 justification must accompany the placement."""
        return self.shifts_sealed

    @property
    def default(self) -> bool:
        """True when only open steps shift (the default placement rule)."""
        return not self.shifts_sealed

    def describe(self) -> str:
        """Readable one-line description for the proposal's placement section."""
        text = f"position {self.position:03d}"
        if self.phase:
            text += f" in phase {self.phase}"
        if self.after_step:
            text += f" after {self.after_step}"
        if self.reason:
            text += f" ({self.reason})"
        if self.sealed_steps_shifted:
            text += f"; shifts sealed steps {', '.join(self.sealed_steps_shifted)}"
        return text


def evaluate_placement(
    plan: RoadmapPlan, position: int, *, phase: str = "", reason: str = ""
) -> PlacementDecision:
    """Describe one candidate placement, including the sealed steps it would move."""
    if isinstance(position, bool) or not isinstance(position, int):
        raise RoadmapChangeError("placement position must be an integer")
    if not 1 <= position <= plan.step_count + 1:
        raise RoadmapChangeError(
            f"placement position {position} is outside 1..{plan.step_count + 1}"
        )
    shifted = plan.sealed_steps_at_or_after(position)
    return PlacementDecision(
        position=position,
        phase=phase,
        reason=reason,
        sealed_steps_shifted=tuple(step.id for step in shifted),
    )


def choose_placement(plan: RoadmapPlan, *, phase: str, after_step: str = "") -> PlacementDecision:
    """Choose the default placement for a new step (CHANGE-WORKFLOW 3.1).

    With an explicit ``after_step`` that position is used. Otherwise the natural
    position at the end of the idea's phase is preferred **when it shifts only
    open steps**; if it would move a sealed step, placement falls back to
    directly before the terminal certification gate, which is the position that
    keeps the numbering of evidence-holding steps stable.
    """
    plan.require_valid()
    if after_step:
        anchor = plan.step(after_step)
        decision = evaluate_placement(
            plan, anchor.n + 1, phase=phase or anchor.phase, reason=f"after {after_step}"
        )
        return replace(decision, after_step=after_step)
    if phase and not plan.phase_steps(phase):
        raise TriageError(
            f"phase {phase!r} has no step yet in this plan: add the phase in the same "
            "revision or place the step explicitly with after_step"
        )
    terminal = plan.terminal_step()
    terminal_position = terminal.n if terminal is not None else plan.step_count + 1
    candidates: list[PlacementDecision] = []
    if phase:
        last = plan.phase_steps(phase)[-1]
        candidates.append(
            evaluate_placement(
                plan,
                min(last.n + 1, terminal_position),
                phase=phase,
                reason=f"end of phase {phase}",
            )
        )
    candidates.append(
        evaluate_placement(
            plan,
            terminal_position,
            phase=phase,
            reason="before the terminal certification gate",
        )
    )
    for candidate in candidates:
        if candidate.default:
            return candidate
    return candidates[0]


def _renumber(steps: Sequence[RoadmapStep]) -> tuple[RoadmapStep, ...]:
    return tuple(replace(step, n=index) for index, step in enumerate(steps, 1))


def _sealed_shifts(
    before: RoadmapPlan, renumbered: Sequence[RoadmapStep]
) -> tuple[NumberShift, ...]:
    previous = {step.id: step for step in before.steps}
    shifts: list[NumberShift] = []
    for step in renumbered:
        old = previous.get(step.id)
        if old is not None and old.n != step.n and old.sealed:
            shifts.append(NumberShift(step.id, old.n, step.n, old.state))
    return tuple(shifts)


def _renumber_reason(justification: str, shifts: Sequence[NumberShift]) -> str:
    """Validate and normalize an R1/R2/R3 justification for sealed shifts."""
    text = _single_line(justification)
    if not shifts:
        return ""
    if not text:
        ids = ", ".join(shift.step_id for shift in shifts)
        legend = "; ".join(f"{code} {RENUMBER_REASON_LEGEND[code]}" for code in RENUMBER_REASONS)
        raise FrozenStepError(
            f"renumbering sealed step(s) {ids} needs an R1/R2/R3 justification ({legend})"
        )
    code = text.split(" ", 1)[0].upper()
    if code not in RENUMBER_REASONS:
        raise FrozenStepError(
            f"renumber justification {text!r} must start with one of "
            f"{', '.join(RENUMBER_REASONS)}"
        )
    return text


def _justification_text(shifts: Sequence[NumberShift], reason: str) -> tuple[tuple[str, str], ...]:
    return tuple(
        (shift.step_id, f"{shift.old:03d} -> {shift.new:03d}: {reason}") for shift in shifts
    )


def _merge_renumbered(
    carried: Sequence[tuple[str, str]], shifts: Sequence[NumberShift], reason: str
) -> tuple[tuple[str, str], ...]:
    """Carry earlier justifications of one revision and add this edit's shifts.

    A step that shifts again inside the same revision keeps a single record (the
    later, more recent numbers). The authoritative old -> new mapping for the
    revision is :class:`RenumberPreview` computed from the revision's base plan.
    """
    if not shifts or not reason:
        return tuple(carried)
    merged = dict(carried)
    for key, value in _justification_text(shifts, reason):
        merged[key] = value
    return tuple(merged.items())


def _merge_removed(
    carried: Sequence[tuple[str, str]], step_id: str, reason: str
) -> tuple[tuple[str, str], ...]:
    """Carry earlier removal reasons of one revision and add this one."""
    merged = dict(carried)
    merged[step_id] = reason
    return tuple(merged.items())


def insert_step(
    plan: RoadmapPlan,
    step: RoadmapStep,
    *,
    position: int,
    justification: str = "",
) -> RoadmapPlan:
    """Insert a new step at a 1-based position and renumber contiguously.

    Only open steps may shift unless ``justification`` starts with an R1/R2/R3
    reason; every sealed step that moves is then recorded in ``renumbered`` with
    its old and new number. The result is validated before it is returned.
    """
    plan.require_valid()
    if isinstance(position, bool) or not isinstance(position, int):
        raise RoadmapChangeError("insert position must be an integer")
    if not 1 <= position <= plan.step_count + 1:
        raise RoadmapChangeError(f"insert position {position} is outside 1..{plan.step_count + 1}")
    if plan.has_step(step.id):
        raise RoadmapChangeError(f"step id {step.id!r} already exists in this plan")
    steps = list(plan.steps)
    steps.insert(position - 1, step)
    numbered = _renumber(steps)
    shifts = _sealed_shifts(plan, numbered)
    reason = _renumber_reason(justification, shifts)
    updated = replace(
        plan,
        steps=numbered,
        renumbered=_merge_renumbered(plan.renumbered, shifts, reason),
    )
    updated.require_valid()
    return updated


def remove_step(
    plan: RoadmapPlan,
    step_id: str,
    *,
    reason: str,
    justification: str = "",
) -> RoadmapPlan:
    """Remove one open step for a stated reason, renumbering the remainder.

    A sealed step is never removable. The reason is recorded in ``removed`` and
    any sealed step that shifts still needs an R1/R2/R3 justification.
    """
    plan.require_valid()
    step = plan.step(step_id)
    if step.sealed:
        raise FrozenStepError(
            f"sealed step {step_id!r} cannot be removed through a roadmap change; "
            "a roadmap change never removes evidence-holding work"
        )
    if not _single_line(reason):
        raise RoadmapChangeError(f"removing {step_id!r} needs a reason for 'removed'")
    steps = [entry for entry in plan.steps if entry.id != step_id]
    numbered = _renumber(steps)
    shifts = _sealed_shifts(plan, numbered)
    reason_text = _renumber_reason(justification, shifts)
    updated = replace(
        plan,
        steps=numbered,
        renumbered=_merge_renumbered(plan.renumbered, shifts, reason_text),
        removed=_merge_removed(plan.removed, step_id, _single_line(reason)),
    )
    updated.require_valid()
    return updated


def expand_step(
    plan: RoadmapPlan,
    step_id: str,
    *,
    outcome_append: str = "",
    exit_append: str = "",
    add_needs: tuple[str, ...] = (),
    add_integration_needs: tuple[str, ...] = (),
) -> RoadmapPlan:
    """Expand an **open** step in place (triage route A), keeping its number.

    A sealed step is never expanded; that is refused instead of silently
    ignored. Dependency lists only gain entries, and the result is validated, so
    an unknown dependency fails closed.
    """
    plan.require_valid()
    step = plan.step(step_id)
    if step.sealed:
        raise FrozenStepError(
            f"sealed step {step_id!r} is never expanded (state {step.state.value}); "
            "capture the work as a new step that consumes it (route B)"
        )
    outcome_text = _single_line(outcome_append)
    exit_text = _single_line(exit_append)
    if not (outcome_text or exit_text) and not add_needs and not add_integration_needs:
        raise RoadmapChangeError(f"expanding {step_id!r} needs at least one change")
    updated_step = replace(
        step,
        outcome=_append_text(step.outcome, outcome_text),
        exit_criteria=_append_text(step.exit_criteria, exit_text),
        needs=step.needs + tuple(add_needs),
        integration_needs=step.integration_needs + tuple(add_integration_needs),
    )
    updated = replace(
        plan,
        steps=tuple(updated_step if entry.id == step_id else entry for entry in plan.steps),
    )
    updated.require_valid()
    return updated


def _append_text(base: str, extra: str) -> str:
    if not extra:
        return base
    return f"{base.rstrip()} {extra}".strip() if base.strip() else extra


def _suffix(base: str, fresh: str) -> str:
    """The part of ``fresh`` beyond ``base`` (whole text when not a prefix)."""
    return fresh[len(base) :].strip() if fresh.startswith(base) else fresh


# ---------------------------------------------------------------------------
# In-flight lanes and file-scope collisions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InFlightLane:
    """One running lane observed in the live worktree/lease inventory."""

    lane_id: str
    step_id: str = ""
    touches: tuple[str, ...] = ()
    worktree: str = ""
    branch: str = ""
    state: str = ""

    def __post_init__(self) -> None:
        """Canonicalize identity and declared write scopes."""
        object.__setattr__(self, "lane_id", _clean(self.lane_id))
        object.__setattr__(self, "step_id", _clean(self.step_id))
        if not self.lane_id:
            raise RoadmapChangeError("every in-flight lane needs a non-empty lane_id")
        object.__setattr__(self, "touches", _dedup(self.touches))


@dataclass(frozen=True)
class ScopeCollision:
    """One idea path that overlaps a running lane's declared write scope."""

    lane_id: str
    step_id: str
    path: str
    scope: str
    worktree: str = ""
    branch: str = ""

    def describe(self) -> str:
        """Readable one-line description for the impact table."""
        lane = f"{self.lane_id} ({self.step_id})" if self.step_id else self.lane_id
        return f"{self.path} overlaps {self.scope} owned by lane {lane}"


def detect_collisions(
    paths: Iterable[str], lanes: Iterable[InFlightLane]
) -> tuple[ScopeCollision, ...]:
    """Return every overlap between intended paths and running lanes' scopes."""
    wanted = tuple(_require_scope(path, "path") for path in paths)
    found: list[ScopeCollision] = []
    for lane in lanes:
        for scope in lane.touches:
            canonical = _require_scope(scope, f"lane {lane.lane_id} scope")
            for path in wanted:
                if _scope_owns(path, canonical):
                    found.append(
                        ScopeCollision(
                            lane_id=lane.lane_id,
                            step_id=lane.step_id,
                            path=path,
                            scope=canonical,
                            worktree=lane.worktree,
                            branch=lane.branch,
                        )
                    )
    return tuple(sorted(found, key=lambda item: (item.lane_id, item.path, item.scope)))


# ---------------------------------------------------------------------------
# Idea intake (stage 0)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NewStepSpec:
    """The definition of a new step an idea asks for (routes B and C)."""

    id: str
    title: str = ""
    phase: str = ""
    outcome: str = ""
    exit_criteria: str = ""
    needs: tuple[str, ...] = ()
    integration_needs: tuple[str, ...] = ()
    optional: bool = False
    owns: tuple[str, ...] = ()
    size: str = DEFAULT_STEP_SIZE
    after_step: str = ""
    justification: str = ""

    def __post_init__(self) -> None:
        """Canonicalize identity and planning fields, failing closed."""
        object.__setattr__(self, "id", _clean(self.id))
        if not self.id:
            raise RoadmapChangeError("a new step needs a stable non-empty id")
        object.__setattr__(self, "needs", _dedup(self.needs))
        object.__setattr__(self, "integration_needs", _dedup(self.integration_needs))
        object.__setattr__(self, "owns", _dedup(self.owns))
        size = _clean(self.size).upper()
        if size not in _SIZE_VALUES:
            raise RoadmapChangeError(f"new step {self.id}: unknown size {self.size!r}")
        object.__setattr__(self, "size", size)

    def as_step(self, *, version: int) -> RoadmapStep:
        """Build the ``steps.json`` step row, stamped with the adding version."""
        return RoadmapStep(
            id=self.id,
            title=self.title or self.id,
            phase=self.phase,
            n=0,
            state=StepState.OPEN,
            needs=self.needs,
            integration_needs=self.integration_needs,
            optional=self.optional,
            outcome=self.outcome,
            exit_criteria=self.exit_criteria,
            added=version,
            owns=self.owns,
            size=self.size,
        )


@dataclass(frozen=True)
class RoadmapIdea:
    """One captured roadmap idea plus the Orchestrator's triage of it.

    ``words`` is the owner's exact text and is never normalized. Every other
    field is written into the idea file's front matter, so a file written by
    :func:`capture_idea` and read back by :func:`read_idea` keeps those fields
    stable. Front-matter scalars are intentionally single-line; the owner's
    words keep their original line breaks in the body.
    """

    idea_id: str
    slug: str = ""
    title: str = ""
    captured_at: str = ""
    by: str = "owner"
    words: str = ""
    status: IdeaState = IdeaState.CAPTURED
    route: TriageRoute | None = None
    target_step: str = ""
    covered_by: str = ""
    change_text: str = ""
    needs: tuple[str, ...] = ()
    touch_paths: tuple[str, ...] = ()
    justification: str = ""
    until: str = ""
    reason: str = ""
    decision: str = ""
    triage_note: str = ""
    new_step: NewStepSpec | None = None

    def __post_init__(self) -> None:
        """Canonicalize identity, preserving the owner's words verbatim."""
        object.__setattr__(self, "idea_id", _clean(self.idea_id))
        if not self.idea_id:
            raise RoadmapChangeError("every idea needs a non-empty idea_id (its file stem)")
        object.__setattr__(self, "slug", _clean(self.slug) or _slug_from_idea_id(self.idea_id))
        object.__setattr__(self, "by", _clean(self.by) or "owner")
        object.__setattr__(self, "target_step", _clean(self.target_step))
        object.__setattr__(self, "covered_by", _clean(self.covered_by))
        object.__setattr__(self, "touch_paths", _dedup(self.touch_paths))
        object.__setattr__(self, "needs", _dedup(self.needs))

    @property
    def words_verbatim(self) -> str:
        """The owner's exact words, preserved."""
        return self.words

    def front_matter(self) -> tuple[tuple[str, str], ...]:
        """Ordered ``key: value`` front-matter rows, empty values omitted."""
        rows: list[tuple[str, str]] = [
            ("status", self.status.value),
            ("title", _single_line(self.title) or self.slug),
            ("captured", _single_line(self.captured_at)),
            ("by", _single_line(self.by)),
        ]
        optional: tuple[tuple[str, object], ...] = (
            ("route", self.route.value if self.route is not None else ""),
            ("target_step", self.target_step),
            ("covered_by", self.covered_by),
            ("change_text", self.change_text),
            ("needs", ", ".join(self.needs)),
            ("touch_paths", ", ".join(self.touch_paths)),
            ("justification", self.justification),
            ("until", self.until),
            ("reason", self.reason),
            ("decision", self.decision),
        )
        for key, value in optional:
            text = _single_line(value)
            if text:
                rows.append((key, text))
        spec = self.new_step
        if spec is not None:
            rows.extend(
                (
                    ("new_step_id", spec.id),
                    ("new_step_title", _single_line(spec.title)),
                    ("new_step_phase", spec.phase),
                    ("new_step_outcome", _single_line(spec.outcome)),
                    ("new_step_exit", _single_line(spec.exit_criteria)),
                    ("new_step_needs", ", ".join(spec.needs)),
                    ("new_step_integration_needs", ", ".join(spec.integration_needs)),
                    ("new_step_optional", "true" if spec.optional else "false"),
                    ("new_step_owns", ", ".join(spec.owns)),
                    ("new_step_size", spec.size),
                    ("new_step_after_step", spec.after_step),
                    ("new_step_justification", spec.justification),
                )
            )
        return tuple((key, text) for key, text in rows if text)

    def to_markdown(self) -> str:
        """Render the idea file: front matter, verbatim words, triage section."""
        lines = [f"{key}: {value}" for key, value in self.front_matter()]
        lines.extend(("", _IDEA_SECTION_WORDS, "", (self.words or "").rstrip("\n"), ""))
        lines.append(_IDEA_SECTION_TRIAGE)
        lines.append("")
        note = self.triage_note.strip()
        if note:
            lines.append(note)
        else:
            lines.append(
                "(Orchestrator fills at the next turn boundary: route A-E, "
                "see ROADMAP/CHANGE-WORKFLOW.md)"
            )
        return "\n".join(lines).rstrip("\n") + "\n"

    @classmethod
    def from_markdown(cls, text: str, *, idea_id: str) -> RoadmapIdea:
        """Parse an idea file written by ``orch idea`` or :meth:`to_markdown`."""
        front, words, note = _parse_idea_document(text)
        if not words.strip():
            raise IdeaFormatError(
                f"{idea_id}: missing the {_IDEA_SECTION_WORDS!r} section or its owner words"
            )
        status = _clean(front.get("status", IdeaState.CAPTURED.value)).lower() or "captured"
        try:
            state = IdeaState(status)
        except ValueError as exc:
            raise IdeaFormatError(f"{idea_id}: unknown status {status!r}") from exc
        route = front.get("route", "").strip()
        spec_id = _clean(front.get("new_step_id"))
        spec = (
            NewStepSpec(
                id=spec_id,
                title=front.get("new_step_title", ""),
                phase=front.get("new_step_phase", ""),
                outcome=front.get("new_step_outcome", ""),
                exit_criteria=front.get("new_step_exit", ""),
                needs=_string_tuple(front.get("new_step_needs"), "new_step_needs"),
                integration_needs=_string_tuple(
                    front.get("new_step_integration_needs"), "new_step_integration_needs"
                ),
                optional=_front_bool(front, "new_step_optional"),
                owns=_string_tuple(front.get("new_step_owns"), "new_step_owns"),
                size=front.get("new_step_size", DEFAULT_STEP_SIZE),
                after_step=front.get("new_step_after_step", ""),
                justification=front.get("new_step_justification", ""),
            )
            if spec_id
            else None
        )
        return cls(
            idea_id=idea_id,
            title=front.get("title", ""),
            captured_at=front.get("captured", ""),
            by=front.get("by", "owner") or "owner",
            words=words,
            status=state,
            route=TriageRoute.parse(route) if route else None,
            target_step=front.get("target_step", ""),
            covered_by=front.get("covered_by", ""),
            change_text=front.get("change_text", ""),
            needs=_string_tuple(front.get("needs"), "needs"),
            touch_paths=_string_tuple(front.get("touch_paths"), "touch_paths"),
            justification=front.get("justification", ""),
            until=front.get("until", ""),
            reason=front.get("reason", ""),
            decision=front.get("decision", ""),
            triage_note=note,
            new_step=spec,
        )


def _parse_idea_document(text: str) -> tuple[dict[str, str], str, str]:
    lines = text.splitlines()
    front: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            break
        key, separator, value = line.partition(":")
        if not separator:
            break
        front[key.strip().lower()] = value.strip()
        index += 1

    def section(heading: str) -> str:
        collected: list[str] = []
        started = False
        for line in lines:
            if line.strip() == heading:
                started = True
                continue
            if started and line.startswith("## "):
                break
            if started:
                collected.append(line)
        return "\n".join(collected).strip("\n")

    return front, section(_IDEA_SECTION_WORDS), section(_IDEA_SECTION_TRIAGE)


def idea_path(directory: Path | str, idea_id: str) -> Path:
    """Return the canonical path of one idea file."""
    stem = _clean(idea_id)
    if not stem:
        raise IdeaFormatError("an idea path needs a non-empty idea_id")
    return Path(directory) / f"{stem}.md"


def write_idea(directory: Path | str, idea: RoadmapIdea, *, overwrite: bool = False) -> Path:
    """Write an idea file, refusing to overwrite a captured idea by default."""
    path = idea_path(directory, idea.idea_id)
    if path.exists() and not overwrite:
        raise IdeaFormatError(
            f"{path} already exists: capture is append-only, pass overwrite=True to update triage"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(idea.to_markdown(), encoding="utf-8")
    return path


def read_idea(path: Path | str) -> RoadmapIdea:
    """Read one idea file, using its file stem as the idea identity."""
    resolved = Path(path)
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise IdeaFormatError(f"cannot read idea {resolved}: {exc}") from exc
    return RoadmapIdea.from_markdown(text, idea_id=resolved.stem)


def capture_idea(
    directory: Path | str,
    *,
    slug: str,
    words: str,
    title: str = "",
    by: str = "owner",
    captured_at: datetime | None = None,
) -> RoadmapIdea:
    """Capture one idea verbatim as ``I-<timestamp>-<slug>.md``.

    ``captured_at`` defaults to the current local time (the one clock read in
    this module); pass it explicitly for deterministic tests or replays. The
    owner's words are stored exactly as given.
    """
    stamp = captured_at if captured_at is not None else datetime.now().astimezone()
    slug_text = _slugify(slug)
    idea = RoadmapIdea(
        idea_id=f"I-{stamp:%Y%m%d-%H%M%S}-{slug_text}",
        slug=slug_text,
        title=title or slug_text,
        captured_at=stamp.isoformat(timespec="minutes"),
        by=by,
        words=words,
    )
    write_idea(directory, idea)
    return idea


def load_ideas(directory: Path | str) -> tuple[RoadmapIdea, ...]:
    """Read every idea file in a directory, in deterministic file-name order."""
    folder = Path(directory)
    if not folder.is_dir():
        return ()
    return tuple(read_idea(path) for path in sorted(folder.glob("*.md")))


# ---------------------------------------------------------------------------
# Triage (stage 1, routes A-E)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriageContext:
    """Live facts triage must respect: the in-flight lanes right now."""

    lanes: tuple[InFlightLane, ...] = ()

    def lanes_for_step(self, step_id: str) -> tuple[InFlightLane, ...]:
        """Every in-flight lane working on one step."""
        return tuple(lane for lane in self.lanes if lane.step_id == step_id)


@dataclass(frozen=True)
class TriageDecision:
    """The validated triage of one idea: one route plus its consequences."""

    idea_id: str
    route: TriageRoute
    target_step: str = ""
    needs: tuple[str, ...] = ()
    covered_by: str = ""
    change_text: str = ""
    new_step: NewStepSpec | None = None
    placement: PlacementDecision | None = None
    justification: str = ""
    until: str = ""
    reason: str = ""
    collisions: tuple[ScopeCollision, ...] = ()
    integrate_with: tuple[str, ...] = ()
    held_by: tuple[str, ...] = ()

    @property
    def family(self) -> str:
        """Route family letter (``E`` for every E sub-case)."""
        return self.route.family

    @property
    def creates_step(self) -> bool:
        """True when the route adds a new step to the plan."""
        return self.route.creates_step

    @property
    def changes_plan(self) -> bool:
        """True when the route changes the plan."""
        return self.route.changes_plan

    @property
    def requires_renumber(self) -> bool:
        """True when the placement moves at least one sealed step."""
        return self.placement is not None and self.placement.shifts_sealed

    @property
    def renumber_justification(self) -> str:
        """The R1/R2/R3 justification recorded for a sealed-step shift."""
        return self.justification if self.requires_renumber else ""

    def describe(self) -> str:
        """One-line summary for the proposal's triage table."""
        parts = [f"route {self.route.value}"]
        if self.target_step:
            parts.append(f"target {self.target_step}")
        if self.new_step is not None:
            parts.append(f"new step {self.new_step.id}")
        if self.covered_by:
            parts.append(f"covered by {self.covered_by}")
        if self.until:
            parts.append(f"until {self.until}")
        if self.integrate_with:
            parts.append(f"INTEGRATE {', '.join(self.integrate_with)}")
        if self.held_by:
            parts.append(f"held by {', '.join(self.held_by)}")
        if self.collisions:
            parts.append(f"{len(self.collisions)} unresolved collision(s)")
        return " · ".join(parts)


def _parse_until(value: str) -> str:
    text = _single_line(value)
    if not _DATE_RE.fullmatch(text):
        raise TriageError(f"route E4 needs until: <YYYY-MM-DD>, got {value!r}")
    return text


def _require_known_need(plan: RoadmapPlan, step_id: str, need: str) -> None:
    if plan.has_step(need) or need in plan.external_prereqs:
        return
    raise TriageError(f"{step_id}: unknown dependency {need!r} (not a step or external prereq)")


def _uncovered_collisions(
    collisions: Sequence[ScopeCollision], integration_needs: Sequence[str]
) -> tuple[ScopeCollision, ...]:
    covered = set(integration_needs)
    return tuple(collision for collision in collisions if collision.step_id not in covered)


def triage_idea(
    plan: RoadmapPlan,
    idea: RoadmapIdea,
    context: TriageContext | None = None,
) -> TriageDecision:
    """Validate one idea's declared route against the live plan and lanes.

    This is deterministic software, not judgement: it refuses a route that
    contradicts plan state (expanding a sealed step, route B on an open step, an
    unknown target or dependency, an uncovered file-scope collision with a
    running lane, a missing justification for a sealed renumber) instead of
    quietly reinterpreting it.
    """
    plan.require_valid()
    live = context if context is not None else TriageContext()
    route = idea.route
    if route is None:
        raise TriageError(f"{idea.idea_id}: no triage route recorded (A, B, C, D or E1-E4)")
    collisions = detect_collisions(idea.touch_paths, live.lanes)
    spec = idea.new_step
    if route is TriageRoute.A:
        if not idea.target_step:
            raise TriageError(
                f"{idea.idea_id}: route A needs target_step (the open step it expands)"
            )
        target = plan.step(idea.target_step)
        if target.sealed:
            raise TriageError(
                f"{idea.idea_id}: route A target {target.id} is {target.state.value}; "
                "a sealed step is never expanded (use route B)"
            )
        lanes = live.lanes_for_step(target.id)
        if lanes:
            names = ", ".join(lane.lane_id for lane in lanes)
            raise TriageError(
                f"{idea.idea_id}: route A target {target.id} already has in-flight lane(s) "
                f"{names}; the wave must close first (route E2)"
            )
        if collisions:
            raise TriageError(
                f"{idea.idea_id}: {collisions[0].describe()}; declare that lane's step as an "
                "INTEGRATE dependency (route B) or hold the idea (route E2)"
            )
        for need in idea.needs:
            _require_known_need(plan, target.id, need)
        change_text = _single_line(idea.change_text)
        if not change_text and not idea.needs:
            raise TriageError(
                f"{idea.idea_id}: route A needs change_text: the text the open step gains "
                "(or a needs addition)"
            )
        return TriageDecision(
            idea_id=idea.idea_id,
            route=route,
            target_step=target.id,
            needs=idea.needs,
            change_text=change_text,
            reason=idea.reason,
        )
    if route in (TriageRoute.B, TriageRoute.C):
        if spec is None:
            raise TriageError(f"{idea.idea_id}: route {route.value} needs a new step definition")
        if not spec.phase:
            raise TriageError(
                f"{idea.idea_id}: route {route.value} needs the new step's phase "
                "(a step belongs to one of the plan's phases)"
            )
        needs = spec.needs
        frozen_consumed = ""
        if route is TriageRoute.B:
            if not idea.target_step:
                raise TriageError(
                    f"{idea.idea_id}: route B needs target_step (the sealed step it consumes)"
                )
            consumed = plan.step(idea.target_step)
            if not consumed.sealed:
                raise TriageError(
                    f"{idea.idea_id}: route B consumes a sealed step; {consumed.id} is "
                    f"{consumed.state.value} (use route A to expand it)"
                )
            if consumed.id not in needs:
                raise TriageError(
                    f"{idea.idea_id}: route B must list the consumed sealed step "
                    f"{consumed.id!r} in the new step's needs"
                )
            frozen_consumed = consumed.id
        for need in (*needs, *spec.integration_needs):
            _require_known_need(plan, spec.id, need)
        uncovered = _uncovered_collisions(collisions, spec.integration_needs)
        if uncovered:
            raise TriageError(
                f"{idea.idea_id}: {uncovered[0].describe()}; add the lane's step to the new "
                "step's INTEGRATE dependencies"
            )
        placement = choose_placement(plan, phase=spec.phase, after_step=spec.after_step)
        justification = _single_line(spec.justification or idea.justification)
        if placement.shifts_sealed and not justification:
            shifted = ", ".join(placement.sealed_steps_shifted)
            raise TriageError(
                f"{idea.idea_id}: placement shifts sealed step(s) {shifted}; record an "
                "R1/R2/R3 justification (R1 reading order / R2 phase coherence / R3 consolidation)"
            )
        return TriageDecision(
            idea_id=idea.idea_id,
            route=route,
            target_step=frozen_consumed,
            needs=needs,
            new_step=spec,
            placement=placement,
            justification=justification,
            reason=idea.reason,
            integrate_with=tuple(
                dict.fromkeys(
                    collision.step_id
                    for collision in collisions
                    if collision.step_id in set(spec.integration_needs)
                )
            ),
        )
    if route is TriageRoute.D:
        return TriageDecision(
            idea_id=idea.idea_id,
            route=route,
            change_text=_single_line(idea.change_text),
            reason=idea.reason,
        )
    if route is TriageRoute.E1:
        if not idea.covered_by or not plan.has_step(idea.covered_by):
            raise TriageError(
                f"{idea.idea_id}: route E1 needs covered_by naming an existing step, "
                f"got {idea.covered_by!r}"
            )
        return TriageDecision(
            idea_id=idea.idea_id,
            route=route,
            covered_by=idea.covered_by,
            reason=idea.reason,
        )
    if route is TriageRoute.E2:
        held = tuple(dict.fromkeys(collision.lane_id for collision in collisions))
        if not held and idea.target_step:
            held = tuple(lane.lane_id for lane in live.lanes_for_step(idea.target_step))
        if not held:
            raise TriageError(
                f"{idea.idea_id}: route E2 needs a conflict with in-flight work "
                "(no overlapping path or lane is recorded)"
            )
        if not _single_line(idea.reason):
            raise TriageError(f"{idea.idea_id}: route E2 needs the conflict stated in reason")
        return TriageDecision(
            idea_id=idea.idea_id,
            route=route,
            target_step=idea.target_step,
            reason=_single_line(idea.reason),
            collisions=collisions,
            held_by=held,
        )
    if route is TriageRoute.E3:
        if not _single_line(idea.reason):
            raise TriageError(f"{idea.idea_id}: route E3 (reject) needs the reason")
        return TriageDecision(
            idea_id=idea.idea_id,
            route=route,
            target_step=idea.target_step,
            reason=_single_line(idea.reason),
        )
    if not _single_line(idea.until):
        raise TriageError(f"{idea.idea_id}: route E4 (later) needs until: <YYYY-MM-DD>")
    return TriageDecision(
        idea_id=idea.idea_id,
        route=route,
        target_step=idea.target_step,
        until=_parse_until(idea.until),
        reason=idea.reason,
    )


def triage_batch(
    plan: RoadmapPlan,
    ideas: Sequence[RoadmapIdea],
    context: TriageContext | None = None,
) -> tuple[TriageDecision, ...]:
    """Triage every captured idea in one pass at a turn boundary.

    Step-creating decisions are threaded through a working plan, so the second
    new step of a batch is placed and numbered as if the first were already
    applied: one revision, at most one renumber, no self-inflicted collision.
    """
    plan.require_valid()
    working = plan
    decisions: list[TriageDecision] = []
    for idea in ideas:
        decision = triage_idea(working, idea, context)
        decisions.append(decision)
        if (
            decision.creates_step
            and decision.new_step is not None
            and decision.placement is not None
        ):
            working = insert_step(
                working,
                decision.new_step.as_step(version=plan.next_version),
                position=decision.placement.position,
                justification=decision.renumber_justification,
            )
    return tuple(decisions)


# ---------------------------------------------------------------------------
# Deterministic impact check (stage 2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostImpact:
    """Expected planning cost of a revision (CHANGE-WORKFLOW stage 2, "Cost")."""

    new_steps: int = 0
    expanded_steps: int = 0
    shifted_open_steps: int = 0
    shifted_sealed_steps: int = 0
    worker_sessions: int = 0
    reviewer_sessions: int = 0
    integrator_sessions: int = 0
    gate_runs: int = 0
    owner_decisions: int = 0

    @property
    def total_sessions(self) -> int:
        """Worker + Reviewer + Integrator sessions."""
        return self.worker_sessions + self.reviewer_sessions + self.integrator_sessions

    def describe(self) -> str:
        """Readable cost line, with the do-nothing alternative named."""
        return (
            f"{self.total_sessions} session(s) "
            f"({self.worker_sessions}W/{self.reviewer_sessions}R/{self.integrator_sessions}I), "
            f"{self.gate_runs} gate run(s), {self.owner_decisions} owner decision(s); "
            "doing nothing: 0"
        )


@dataclass(frozen=True)
class MakespanImpact:
    """Makespan delta of the remaining (not ``done``) plan, by WP-1's scoring."""

    base_makespan: float = 0.0
    revised_makespan: float = 0.0
    added_tasks: int = 0
    removed_tasks: int = 0

    @property
    def delta(self) -> float:
        """Revised minus base makespan (0.0 when a renumber changes nothing)."""
        return round(self.revised_makespan - self.base_makespan, 6)

    def describe(self) -> str:
        """Readable makespan line."""
        return (
            f"makespan {self.base_makespan:g} -> {self.revised_makespan:g} "
            f"(delta {self.delta:+g}), tasks +{self.added_tasks}/-{self.removed_tasks}"
        )


@dataclass(frozen=True)
class ImpactReport:
    """The full stage-2 impact table plus the deterministic verdict."""

    ideas: tuple[str, ...] = ()
    steps_touched: tuple[str, ...] = ()
    sealed_steps_touched: tuple[str, ...] = ()
    expanded_steps: tuple[str, ...] = ()
    added_steps: tuple[str, ...] = ()
    removed_steps: tuple[str, ...] = ()
    dependencies: tuple[tuple[str, str], ...] = ()
    collisions: tuple[ScopeCollision, ...] = ()
    integrate_with: tuple[str, ...] = ()
    renumber: RenumberPreview = field(default_factory=RenumberPreview)
    errors: tuple[str, ...] = ()
    violations: tuple[FrozenViolation, ...] = ()
    adr_notes: tuple[str, ...] = ()
    cost: CostImpact = field(default_factory=CostImpact)
    makespan: MakespanImpact = field(default_factory=MakespanImpact)
    running_lane_conflicts: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """True when nothing blocks the revision."""
        return (
            not self.errors
            and not self.violations
            and not self.collisions
            and not self.running_lane_conflicts
        )

    def check_rows(self) -> tuple[tuple[str, str], ...]:
        """The CHANGE-WORKFLOW section 2 impact rows, every row answered."""
        states = ", ".join(self.steps_touched) or "none"
        guard = (
            "PASS (no sealed step changed)"
            if not self.violations and not self.errors
            else "; ".join(
                [violation.describe() for violation in self.violations] + list(self.errors)
            )
        )
        lane_parts = [collision.describe() for collision in self.collisions]
        if self.integrate_with:
            lane_parts.append(
                f"resolved by INTEGRATE dependency on {', '.join(self.integrate_with)}"
            )
        deps = ", ".join(f"{step} needs {need}" for step, need in self.dependencies) or "none"
        adr = "; ".join(self.adr_notes) or "none"
        values = (
            states,
            guard,
            "; ".join(lane_parts) or "none",
            deps,
            adr,
            "; ".join(self.renumber.summary_lines()),
            self.cost.describe(),
            self.makespan.describe(),
        )
        return tuple(zip(IMPACT_ROWS, values, strict=True))

    def require_ok(self) -> None:
        """Raise the specific refusal error when the impact is not clean."""
        if self.violations or self.errors:
            reasons = "; ".join(
                [violation.describe() for violation in self.violations] + list(self.errors)
            )
            raise FrozenStepError(f"roadmap change refused: {reasons}")
        if self.running_lane_conflicts:
            raise RunningLaneError(
                "revision would touch step(s) with in-flight lanes: "
                + ", ".join(self.running_lane_conflicts)
            )
        if self.collisions:
            raise TriageError(
                "revision collides with in-flight lanes: "
                + "; ".join(collision.describe() for collision in self.collisions)
            )


#: Step fields compared when deciding whether a step's *content* changed.
TOUCHED_STEP_FIELDS: tuple[str, ...] = (
    "title",
    "phase",
    "outcome",
    "exit_criteria",
    "needs",
    "integration_needs",
    "optional",
    "owns",
    "size",
)


def _touched_fields(step: RoadmapStep) -> tuple[object, ...]:
    return tuple(step.field_value(name) for name in TOUCHED_STEP_FIELDS)


def steps_touched(prev: RoadmapPlan, new: RoadmapPlan) -> tuple[str, ...]:
    """Stable IDs whose content (not display number) differs between two plans."""
    previous = {step.id: step for step in prev.steps}
    current = {step.id: step for step in new.steps}
    touched: list[str] = []
    for step_id, step in current.items():
        old = previous.get(step_id)
        if old is None or _touched_fields(old) != _touched_fields(step):
            touched.append(step_id)
    touched.extend(step_id for step_id in previous if step_id not in current)
    return tuple(touched)


def running_lane_conflicts(
    prev: RoadmapPlan, new: RoadmapPlan, lanes: Iterable[InFlightLane]
) -> tuple[str, ...]:
    """Lane IDs a revision would disturb: their step is changed or removed."""
    changed = set(steps_touched(prev, new))
    conflicts = [lane.lane_id for lane in lanes if lane.step_id and lane.step_id in changed]
    return tuple(dict.fromkeys(conflicts))


def roadmap_graph(
    plan: RoadmapPlan,
    *,
    project_id: str = DEFAULT_ROADMAP_PROJECT_ID,
    include_done: bool = False,
) -> TaskGraph:
    """Adapt a roadmap into WP-1's task graph (remaining work by default).

    ``done`` steps are satisfied prerequisites and are dropped unless
    ``include_done`` is set; a dependency on a dropped step disappears with it.
    External prerequisites are not nodes, so their edges are dropped too.
    """
    plan.require_valid()
    kept = [step for step in plan.steps if include_done or step.state is not StepState.DONE]
    ids = {step.id for step in kept}
    nodes = tuple(
        PlanNode(
            project_id=project_id,
            task_id=step.id,
            size=step.size,
            title=step.title,
            phase=step.phase,
            start_dependencies=tuple(
                TaskRef(project_id, need) for need in step.needs if need in ids
            ),
            integration_prerequisites=tuple(
                TaskRef(project_id, need) for need in step.integration_needs if need in ids
            ),
            owns=step.owns,
        )
        for step in kept
    )
    return TaskGraph(nodes=nodes)


def roadmap_makespan(
    plan: RoadmapPlan,
    *,
    project_id: str = DEFAULT_ROADMAP_PROJECT_ID,
    limits: OptimizationLimits | None = None,
    include_done: bool = False,
) -> float:
    """Score the remaining plan with WP-1 and return its final makespan."""
    graph = roadmap_graph(plan, project_id=project_id, include_done=include_done)
    return optimize_plan(graph, limits=limits).final_score.makespan


def _cost_impact(
    added: Sequence[str], expanded: Sequence[str], renumber: RenumberPreview
) -> CostImpact:
    new_steps = len(added)
    expanded_steps = len(expanded)
    new_costs = COST_SESSIONS_PER_NEW_STEP
    expansion_costs = COST_SESSIONS_PER_EXPANSION
    return CostImpact(
        new_steps=new_steps,
        expanded_steps=expanded_steps,
        shifted_open_steps=sum(1 for shift in renumber.shifted if not shift.sealed),
        shifted_sealed_steps=len(renumber.sealed_shifted),
        worker_sessions=(
            new_steps * new_costs["worker"] + expanded_steps * expansion_costs["worker"]
        ),
        reviewer_sessions=(
            new_steps * new_costs["reviewer"] + expanded_steps * expansion_costs["reviewer"]
        ),
        integrator_sessions=(
            new_steps * new_costs["integrator"] + expanded_steps * expansion_costs["integrator"]
        ),
        gate_runs=(
            new_steps * new_costs["gate_runs"] + expanded_steps * expansion_costs["gate_runs"]
        ),
        owner_decisions=1 if (new_steps or expanded_steps) else 0,
    )


def _added_dependencies(prev: RoadmapPlan, new: RoadmapPlan) -> tuple[tuple[str, str], ...]:
    previous = {step.id: step for step in prev.steps}
    pairs: list[tuple[str, str]] = []
    for step in new.steps:
        old = previous.get(step.id)
        for need in (*step.needs, *step.integration_needs):
            if old is None or need not in (*old.needs, *old.integration_needs):
                pairs.append((step.id, need))
    return tuple(pairs)


def impact_check(
    prev: RoadmapPlan,
    new: RoadmapPlan,
    *,
    ideas: Sequence[str] = (),
    lanes: Sequence[InFlightLane] = (),
    paths: Sequence[str] = (),
    integrate_with: Sequence[str] = (),
    adr_notes: Sequence[str] = (),
    project_id: str = DEFAULT_ROADMAP_PROJECT_ID,
    limits: OptimizationLimits | None = None,
) -> ImpactReport:
    """Compute the full deterministic impact of a proposed plan revision."""
    check = check_plan_change(prev, new)
    touched = steps_touched(prev, new)
    previous = {step.id: step for step in prev.steps}
    current = {step.id: step for step in new.steps}
    live = tuple(lanes)
    covered = tuple(dict.fromkeys(integrate_with))
    collisions = _uncovered_collisions(detect_collisions(paths, live), covered)
    base_graph = roadmap_graph(prev, project_id=project_id)
    new_graph = roadmap_graph(new, project_id=project_id)
    return ImpactReport(
        ideas=tuple(ideas),
        steps_touched=touched,
        sealed_steps_touched=tuple(
            step_id for step_id in touched if (current.get(step_id) or previous[step_id]).sealed
        ),
        expanded_steps=tuple(
            step_id
            for step_id in touched
            if step_id in previous
            and step_id in current
            and previous[step_id].state is StepState.OPEN
        ),
        added_steps=tuple(step_id for step_id, _ in check.renumber.added),
        removed_steps=check.renumber.removed,
        dependencies=_added_dependencies(prev, new),
        collisions=collisions,
        integrate_with=covered,
        renumber=check.renumber,
        errors=check.errors,
        violations=check.violations,
        adr_notes=tuple(_single_line(note) for note in adr_notes if _single_line(note)),
        cost=_cost_impact(
            tuple(step_id for step_id, _ in check.renumber.added),
            tuple(
                step_id
                for step_id in touched
                if step_id in previous
                and step_id in current
                and previous[step_id].state is StepState.OPEN
            ),
            check.renumber,
        ),
        makespan=MakespanImpact(
            base_makespan=optimize_plan(base_graph, limits=limits).final_score.makespan,
            revised_makespan=optimize_plan(new_graph, limits=limits).final_score.makespan,
            added_tasks=max(0, len(new_graph.nodes) - len(base_graph.nodes)),
            removed_tasks=max(0, len(base_graph.nodes) - len(new_graph.nodes)),
        ),
        running_lane_conflicts=running_lane_conflicts(prev, new, live),
    )


# ---------------------------------------------------------------------------
# Revision proposal (stages 4-5) and revision lock (stage 6)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RevisionProposal:
    """One owner-facing proposal plus the plan revision it would apply."""

    proposal_id: str
    date: str
    project_id: str
    base_plan: RoadmapPlan
    plan: RoadmapPlan
    impact: ImpactReport = field(default_factory=ImpactReport)
    ideas: tuple[RoadmapIdea, ...] = ()
    decisions: tuple[TriageDecision, ...] = ()
    lanes: tuple[InFlightLane, ...] = ()
    adr_notes: tuple[str, ...] = ()
    owner_decision: str = ""
    approved: bool = False

    @property
    def version(self) -> int:
        """Version the applied revision would carry."""
        return self.plan.version

    @property
    def status(self) -> str:
        """``approved`` only when an owner decision is recorded."""
        return "approved" if self.approved and self.owner_decision.strip() else "proposed"

    @property
    def filename(self) -> str:
        """Proposal file name (``<date>-v<N>-<slug>.md``)."""
        return f"{self.proposal_id}.md"

    @property
    def title(self) -> str:
        """Short human title of the proposal (first idea's title or slug)."""
        if self.ideas:
            return self.ideas[0].title or self.ideas[0].slug
        return self.proposal_id

    @property
    def needs_renumber(self) -> bool:
        """True when the revision moves at least one step's display number."""
        return bool(self.impact.renumber.shifted)

    def as_markdown(self) -> str:
        """Render the proposal in ``ROADMAP/proposals/_template.md`` shape."""
        lines = [
            f"status: {self.status}",
            f"revision: v{self.plan.version}",
            f"date: {self.date}",
            f"ideas: {', '.join(idea.idea_id for idea in self.ideas) or 'none'}",
            "",
            f"# Roadmap change proposal — {self.title}",
            "",
            "## Owner words (verbatim, from each idea)",
            "",
        ]
        for idea in self.ideas:
            lines.append(f"- {idea.idea_id}: {json.dumps(idea.words_verbatim)}")
        lines.extend(
            (
                "",
                "## Triage",
                "",
                "| Idea | Route | Target (stable ID) | Why this route |",
                "|---|---|---|---|",
            )
        )
        for decision in self.decisions:
            target = decision.new_step.id if decision.new_step is not None else decision.target_step
            lines.append(
                f"| {decision.idea_id} | {decision.route.value} | {target or '—'} | "
                f"{decision.describe()} |"
            )
        lines.extend(("", "## Changes", ""))
        previous = {step.id: step for step in self.base_plan.steps}
        current = {step.id: step for step in self.plan.steps}
        for step in self.plan.steps:
            if step.id not in previous:
                lines.append(f"**{step.id} (new, {step.phase}, step {step.n:03d}).** {step.title}")
                lines.append(f"Outcome: {step.outcome}")
                lines.append(f"Exit: {step.exit_criteria}")
                lines.append("")
        for step_id in steps_touched(self.base_plan, self.plan):
            old = previous.get(step_id)
            fresh = current.get(step_id)
            if old is None or fresh is None:
                continue
            if old.outcome != fresh.outcome:
                lines.append(
                    f"**{step_id} (open) — outcome gains:** {_suffix(old.outcome, fresh.outcome)}"
                )
            if old.exit_criteria != fresh.exit_criteria:
                lines.append(
                    f"**{step_id} (open) — exit gains:** "
                    f"{_suffix(old.exit_criteria, fresh.exit_criteria)}"
                )
            if old.needs != fresh.needs:
                lines.append(f"**{step_id} (open) — needs:** {', '.join(fresh.needs)}")
            if old.integration_needs != fresh.integration_needs:
                lines.append(
                    f"**{step_id} (open) — INTEGRATE needs:** "
                    f"{', '.join(fresh.integration_needs)}"
                )
        lines.extend(("", '## Impact check (CHANGE-WORKFLOW §2; "none" is an answer)', ""))
        lines.extend(("| Check | Result |", "|---|---|"))
        lines.extend(f"| {row} | {value} |" for row, value in self.impact.check_rows())
        lines.extend(("", "## Placement and renumber justification (R1 / R2 / R3)", ""))
        placements = [decision for decision in self.decisions if decision.placement is not None]
        if not placements:
            lines.append("none needed")
        for decision in placements:
            placement = decision.placement
            if placement is None:
                continue
            lines.append(f"- {decision.idea_id}: {placement.describe()}")
            if placement.needs_justification:
                new_id = decision.new_step.id if decision.new_step is not None else ""
                recorded = self.plan.renumbered_map().get(new_id, "")
                lines.append(f"  - justification: {recorded or decision.renumber_justification}")
        lines.extend(
            ("", "## Owner decision (verbatim)", "", self.owner_decision.strip() or "(pending)", "")
        )
        return "\n".join(lines)


def build_proposal(
    plan: RoadmapPlan,
    ideas: Sequence[RoadmapIdea],
    *,
    date: str,
    slug: str = "",
    lanes: Sequence[InFlightLane] = (),
    adr_notes: Sequence[str] = (),
    owner_decision: str = "",
    approved: bool = False,
    project_id: str = DEFAULT_ROADMAP_PROJECT_ID,
    limits: OptimizationLimits | None = None,
) -> RevisionProposal:
    """Triage a batch of ideas and turn them into one plan revision proposal.

    The proposal is refused (raising) when it would change a sealed step,
    collide with an in-flight lane, touch a step that has a running lane, or
    change nothing at all; that is the deterministic form of "running lanes are
    never touched".
    """
    plan.require_valid()
    if not ideas:
        raise RoadmapChangeError("a roadmap change proposal needs at least one idea")
    live = tuple(lanes)
    decisions = triage_batch(plan, ideas, TriageContext(lanes=live))
    if not any(decision.changes_plan for decision in decisions):
        routes = ", ".join(sorted({decision.route.value for decision in decisions}))
        raise RoadmapChangeError(
            f"no plan change to propose: every idea is route {routes} "
            "(process-only or not-now ideas need no revision)"
        )
    revised = replace(plan, renumbered=(), removed=())
    for decision in decisions:
        if (
            decision.creates_step
            and decision.new_step is not None
            and decision.placement is not None
        ):
            revised = insert_step(
                revised,
                decision.new_step.as_step(version=plan.next_version),
                position=decision.placement.position,
                justification=decision.renumber_justification,
            )
        elif decision.route is TriageRoute.A:
            revised = expand_step(
                revised,
                decision.target_step,
                outcome_append=decision.change_text,
                add_needs=decision.needs,
            )
    revised = replace(
        revised,
        version=plan.next_version,
        date=date,
        note=f"roadmap change requests: {', '.join(idea.idea_id for idea in ideas)}",
    )
    revised.require_valid()
    report = impact_check(
        plan,
        revised,
        ideas=tuple(idea.idea_id for idea in ideas),
        lanes=live,
        paths=tuple(path for idea in ideas for path in idea.touch_paths),
        integrate_with=tuple(
            dict.fromkeys(
                step_id for decision in decisions for step_id in decision.integrate_with
            )
        ),
        adr_notes=adr_notes,
        project_id=project_id,
        limits=limits,
    )
    report.require_ok()
    slug_text = _slugify(slug or ideas[0].slug, fallback="change")
    return RevisionProposal(
        proposal_id=f"{date}-v{revised.version}-{slug_text}",
        date=date,
        project_id=project_id,
        base_plan=plan,
        plan=revised,
        impact=report,
        ideas=tuple(ideas),
        decisions=decisions,
        lanes=live,
        adr_notes=report.adr_notes,
        owner_decision=owner_decision,
        approved=approved,
    )


class RevisionLock:
    """Exclusive ``ROADMAP/.revision.lock`` guard for one revision at a time.

    The lock is created with ``O_CREAT|O_EXCL`` semantics and always released on
    exit, so a failed apply cannot leave a stale revision in progress.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        who: str,
        proposal: str,
        created_at: str = "",
    ) -> None:
        self._path = Path(path)
        self._who = _single_line(who) or "orchestrator"
        self._proposal = _single_line(proposal)
        self._created_at = _single_line(created_at)
        self._held = False

    @property
    def path(self) -> Path:
        """Lock file path."""
        return self._path

    @property
    def held(self) -> bool:
        """True while this instance holds the lock."""
        return self._held

    def acquire(self) -> None:
        """Create the lock exclusively, refusing a concurrent revision."""
        if self._held:
            raise RevisionLockError(f"this RevisionLock already holds {self._path}")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = self._path.open("x", encoding="utf-8")
        except FileExistsError as exc:
            existing = "(unreadable)"
            try:
                existing = self._path.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            raise RevisionLockError(
                f"a roadmap revision is already in progress: {self._path} exists [{existing}]"
            ) from exc
        with handle:
            handle.write(
                f"who: {self._who}\nwhen: {self._created_at}\nproposal: {self._proposal}\n"
            )
        self._held = True

    def release(self) -> None:
        """Remove the lock; a lock this instance does not hold is left alone."""
        if self._held:
            self._path.unlink(missing_ok=True)
            self._held = False

    def __enter__(self) -> RevisionLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.release()


def default_revision_lock_path(roadmap_dir: Path | str) -> Path:
    """Return ``ROADMAP/.revision.lock`` for a roadmap directory."""
    return Path(roadmap_dir) / REVISION_LOCK_FILENAME


@dataclass(frozen=True)
class AppliedRevision:
    """Receipt of one applied proposal: the immutable revision plus its plan."""

    proposal_id: str
    revision: PlanRevision
    plan: RoadmapPlan
    activated: bool
    lock_path: str
    task_count: int


def to_plan_task_defs(
    plan: RoadmapPlan,
    *,
    project_id: str = DEFAULT_ROADMAP_PROJECT_ID,
    external_project: str | None = None,
) -> tuple[PlanTaskDef, ...]:
    """Adapt a roadmap into PL-1's task definitions for the plan store.

    ``needs`` become hard START dependencies and ``integration_needs`` deferred
    INTEGRATE dependencies. An external prerequisite (``external_prereqs``) is
    dropped unless ``external_project`` names the project that owns it, in which
    case it becomes the cross-project reference ``<external_project>/<need>``.
    """
    plan.require_valid()
    if external_project is not None and _PROJECT_SLUG_RE.fullmatch(external_project) is None:
        raise RoadmapChangeError(f"external_project {external_project!r} must be a lowercase slug")
    ids = set(plan.step_ids)
    externals = set(plan.external_prereqs)

    def edges(needs: Sequence[str]) -> tuple[str, ...]:
        resolved: list[str] = []
        for need in needs:
            if need in ids:
                resolved.append(need)
            elif need in externals and external_project is not None:
                resolved.append(f"{external_project}/{need}")
        return tuple(resolved)

    return tuple(
        PlanTaskDef(
            task_id=step.id,
            title=step.title,
            phase=step.phase,
            start_dependencies=edges(step.needs),
            integration_prerequisites=edges(step.integration_needs),
            owns=step.owns,
        )
        for step in plan.steps
    )


def apply_revision(
    store: PlanStore,
    proposal: RevisionProposal,
    *,
    lock_path: Path | str,
    who: str = "orchestrator",
    created_at: str | None = None,
    activate: bool = False,
) -> AppliedRevision:
    """Apply an owner-approved proposal as one immutable plan revision.

    Requires the recorded owner decision, re-verifies the impact check, takes the
    exclusive revision lock, writes one new immutable ``v<N>`` revision through
    PL-1's :class:`~saberops.project_scope.plan_store.PlanStore` and optionally
    activates it. Nothing outside the plan store and the lock file is written;
    no lane prompt, worktree, branch or gate is touched.
    """
    if not proposal.approved or not proposal.owner_decision.strip():
        raise RevisionNotApprovedError(
            "a roadmap revision is applied only after the owner's decision: "
            "set approved=True with the owner's verbatim words"
        )
    proposal.impact.require_ok()
    tasks = to_plan_task_defs(proposal.plan, project_id=proposal.project_id)
    version = f"v{proposal.version}"
    with RevisionLock(
        lock_path,
        who=who,
        proposal=proposal.proposal_id,
        created_at=created_at or proposal.date,
    ):
        revision = store.save_revision(
            proposal.project_id, tasks, version=version, created_at=created_at
        )
        if activate:
            store.set_active(proposal.project_id, version)
    return AppliedRevision(
        proposal_id=proposal.proposal_id,
        revision=revision,
        plan=proposal.plan,
        activated=activate,
        lock_path=str(Path(lock_path)),
        task_count=len(tasks),
    )
