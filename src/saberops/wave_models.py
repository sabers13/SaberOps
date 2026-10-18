"""Wave and lane data models plus their deterministic, fail-closed state machines.

Covers: ADR-0004 §3 D2 (wave model, wave and lane state machines) and §4
invariants 2 (overlapping lane file sets are rejected at submit) and 3 (merges
use pinned SHAs).

What this module is
-------------------
Pure, immutable descriptions of one **wave** and its **lanes**, plus the two
deterministic state machines the wave executor drives:

* :class:`WaveState` -- ``PLANNED -> RUNNING -> INTEGRATING -> GATED ->
  AWAITING_ACCEPT -> ACCEPTED`` with ``BLOCKED`` / ``CANCELLED`` escapes.
* :class:`LaneState` -- ``PLANNED -> RUNNING -> REVIEW -> FIX_UP_K /
  REVIEWED_PROVISIONAL -> ACCEPTED`` with ``BLOCKED`` escapes.

The models perform no I/O, spawn no process, read no clock and call no model.
The state machines are exposed as total functions over the enums so both the
executor and its tests reason about exactly one transition table.

Serialization
-------------
:meth:`Wave.as_dict` / :meth:`Wave.from_dict` round-trip through plain JSON,
and :meth:`Wave.as_sqlite_row` / :meth:`Wave.from_sqlite_row` produce one
SQLite-compatible **row** whose columns are primitive scalars / JSON text (the
nested lanes are stored as a ``lanes_json`` text column). Every parse path
validates types fail-closed: a malformed row raises :class:`WaveModelError`
instead of silently producing a half-built wave.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from saberops.gate_scope import GateScopeError, normalize_path

__all__ = [
    "LANE_ALLOWED_TRANSITIONS",
    "LANE_TERMINAL_STATES",
    "MAX_FIX_ROUNDS_PER_FAILURE",
    "MAX_G3_RUNS_PER_WAVE",
    "WAVE_ALLOWED_TRANSITIONS",
    "WAVE_TERMINAL_STATES",
    "Lane",
    "LaneState",
    "Wave",
    "WaveModelError",
    "WaveState",
    "can_transition_lane",
    "can_transition_wave",
    "is_terminal_lane_state",
    "is_terminal_wave_state",
    "transition_lane",
    "transition_wave",
]

#: ADR-0004 D4 budget: at most four G3 runs per wave before the owner is asked.
MAX_G3_RUNS_PER_WAVE = 4

#: ADR-0004 D4 budget: at most two fix rounds per failure before the owner is asked.
MAX_FIX_ROUNDS_PER_FAILURE = 2

_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class WaveModelError(ValueError):
    """A wave or lane model is structurally invalid; nothing may proceed."""


class WaveState(StrEnum):
    """Lifecycle state of one wave (ADR-0004 D2)."""

    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    INTEGRATING = "INTEGRATING"
    GATED = "GATED"
    AWAITING_ACCEPT = "AWAITING_ACCEPT"
    ACCEPTED = "ACCEPTED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class LaneState(StrEnum):
    """Lifecycle state of one lane, mirroring the manual BOARD vocabulary."""

    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    REVIEW = "REVIEW"
    FIX_UP_K = "FIX_UP_K"
    REVIEWED_PROVISIONAL = "REVIEWED_PROVISIONAL"
    ACCEPTED = "ACCEPTED"
    BLOCKED = "BLOCKED"


#: Deterministic wave transition table. ``ACCEPTED``/``CANCELLED`` are terminal;
#: ``BLOCKED`` may resume into ``RUNNING`` (a transient block, e.g. a fixed
#: conflict or a cleared prerequisite) and is otherwise terminal.
WAVE_ALLOWED_TRANSITIONS: Mapping[WaveState, frozenset[WaveState]] = {
    WaveState.PLANNED: frozenset(
        {WaveState.RUNNING, WaveState.BLOCKED, WaveState.CANCELLED}
    ),
    WaveState.RUNNING: frozenset(
        {WaveState.INTEGRATING, WaveState.BLOCKED, WaveState.CANCELLED}
    ),
    WaveState.INTEGRATING: frozenset(
        {WaveState.INTEGRATING, WaveState.GATED, WaveState.BLOCKED, WaveState.CANCELLED}
    ),
    WaveState.GATED: frozenset(
        {WaveState.AWAITING_ACCEPT, WaveState.BLOCKED, WaveState.CANCELLED}
    ),
    WaveState.AWAITING_ACCEPT: frozenset(
        {WaveState.ACCEPTED, WaveState.BLOCKED, WaveState.CANCELLED}
    ),
    WaveState.ACCEPTED: frozenset(),
    WaveState.BLOCKED: frozenset(
        {WaveState.PLANNED, WaveState.RUNNING, WaveState.CANCELLED}
    ),
    WaveState.CANCELLED: frozenset(),
}

#: Deterministic lane transition table. ``ACCEPTED`` is terminal; ``BLOCKED``
#: may resume into ``RUNNING`` with the same worktree and session.
LANE_ALLOWED_TRANSITIONS: Mapping[LaneState, frozenset[LaneState]] = {
    LaneState.PLANNED: frozenset({LaneState.RUNNING, LaneState.BLOCKED}),
    LaneState.RUNNING: frozenset(
        {LaneState.REVIEW, LaneState.BLOCKED, LaneState.ACCEPTED}
    ),
    LaneState.REVIEW: frozenset(
        {
            LaneState.FIX_UP_K,
            LaneState.REVIEWED_PROVISIONAL,
            LaneState.BLOCKED,
        }
    ),
    LaneState.FIX_UP_K: frozenset(
        {LaneState.RUNNING, LaneState.REVIEW, LaneState.BLOCKED}
    ),
    LaneState.REVIEWED_PROVISIONAL: frozenset(
        {LaneState.ACCEPTED, LaneState.FIX_UP_K, LaneState.BLOCKED}
    ),
    LaneState.ACCEPTED: frozenset(),
    LaneState.BLOCKED: frozenset({LaneState.RUNNING}),
}

WAVE_TERMINAL_STATES: frozenset[WaveState] = frozenset(
    {WaveState.ACCEPTED, WaveState.CANCELLED}
)
LANE_TERMINAL_STATES: frozenset[LaneState] = frozenset({LaneState.ACCEPTED})


def _coerce_wave_state(value: object) -> WaveState:
    try:
        return value if isinstance(value, WaveState) else WaveState(str(value))
    except ValueError as exc:
        raise WaveModelError(f"unknown wave state: {str(value)[:64]!r}") from exc


def _coerce_lane_state(value: object) -> LaneState:
    try:
        return value if isinstance(value, LaneState) else LaneState(str(value))
    except ValueError as exc:
        raise WaveModelError(f"unknown lane state: {str(value)[:64]!r}") from exc


def is_terminal_wave_state(state: WaveState | str) -> bool:
    """True when no further transition is legal for this wave state."""
    return _coerce_wave_state(state) in WAVE_TERMINAL_STATES


def is_terminal_lane_state(state: LaneState | str) -> bool:
    """True when no further transition is legal for this lane state."""
    return _coerce_lane_state(state) in LANE_TERMINAL_STATES


def can_transition_wave(current: WaveState | str, target: WaveState | str) -> bool:
    """True when ``current -> target`` is one legal wave transition."""
    source = _coerce_wave_state(current)
    destination = _coerce_wave_state(target)
    return destination in WAVE_ALLOWED_TRANSITIONS[source]


def can_transition_lane(current: LaneState | str, target: LaneState | str) -> bool:
    """True when ``current -> target`` is one legal lane transition."""
    source = _coerce_lane_state(current)
    destination = _coerce_lane_state(target)
    return destination in LANE_ALLOWED_TRANSITIONS[source]


def transition_wave(current: WaveState | str, target: WaveState | str) -> WaveState:
    """Return ``target`` when the transition is legal, else raise.

    Fail-closed: an unknown state name or a transition that is not in
    :data:`WAVE_ALLOWED_TRANSITIONS` raises :class:`WaveModelError` so a caller
    can never skip ``GATED`` or leave a terminal state by accident.
    """
    source = _coerce_wave_state(current)
    destination = _coerce_wave_state(target)
    if destination not in WAVE_ALLOWED_TRANSITIONS[source]:
        raise WaveModelError(f"illegal wave transition {source.value} -> {destination.value}")
    return destination


def transition_lane(current: LaneState | str, target: LaneState | str) -> LaneState:
    """Return ``target`` when the transition is legal, else raise (fail-closed)."""
    source = _coerce_lane_state(current)
    destination = _coerce_lane_state(target)
    if destination not in LANE_ALLOWED_TRANSITIONS[source]:
        raise WaveModelError(f"illegal lane transition {source.value} -> {destination.value}")
    return destination


def _require_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WaveModelError(f"{field_name} must be a non-empty string")
    return value


def _require_str_list(value: object, field_name: str) -> list[str]:
    if not isinstance(value, list):
        raise WaveModelError(f"{field_name} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise WaveModelError(f"{field_name} entries must be non-empty strings")
        result.append(item)
    return result


def _require_scope_list(value: object, field_name: str) -> list[str]:
    """Validate and canonicalize repo-relative scope patterns."""
    raw = _require_str_list(value, field_name)
    try:
        return [normalize_path(item) for item in raw]
    except GateScopeError as exc:
        raise WaveModelError(f"{field_name}: {exc}") from exc


def _is_sha(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA_RE.fullmatch(value))


@dataclass(frozen=True)
class Lane:
    """One lane of a wave: a prompt, its write scope and its dispatch routes.

    ``touches`` / ``must_not_touch`` are canonical repo-relative scope
    patterns. ``retirement_table`` maps a retired test path to the reason, as
    the manual workflow's retirement table does. ``worker_route`` and
    ``reviewer_route`` name the pinned role-chain routes the wave assigns; the
    admission check verifies each has valid posture.
    """

    lane_id: str
    task_id: str
    prompt: str
    touches: list[str] = field(default_factory=list)
    must_not_touch: list[str] = field(default_factory=list)
    contracts: list[str] = field(default_factory=list)
    retirement_table: dict[str, str] = field(default_factory=dict)
    worker_route: str = ""
    reviewer_route: str = ""
    effort: str = ""

    def __post_init__(self) -> None:
        problems = self.problems()
        if problems:
            raise WaveModelError("; ".join(problems))

    def problems(self) -> list[str]:
        """Return deterministic structural problems; empty means valid."""
        problems: list[str] = []
        for name, value in (
            ("lane_id", self.lane_id),
            ("task_id", self.task_id),
            ("prompt", self.prompt),
            ("worker_route", self.worker_route),
            ("reviewer_route", self.reviewer_route),
            ("effort", self.effort),
        ):
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{name} must be a non-empty string")
        if not self.touches:
            problems.append("touches must list at least one path")
        for scope_name, scope_value in (
            ("touches", self.touches),
            ("must_not_touch", self.must_not_touch),
        ):
            if not isinstance(scope_value, list):
                problems.append(f"{scope_name} must be a list of scope patterns")
                continue
            for item in scope_value:
                if not isinstance(item, str) or not item.strip():
                    problems.append(f"{scope_name} entries must be non-empty strings")
                    continue
                try:
                    normalize_path(item)
                except GateScopeError as exc:
                    problems.append(f"{scope_name} entry {item!r}: {exc}")
        if isinstance(self.touches, list):
            if len(set(self.touches)) != len(self.touches):
                problems.append("touches must not repeat a path")
            overlap = sorted(set(self.touches) & set(self.must_not_touch or []))
            if overlap:
                problems.append(f"touches and must_not_touch overlap: {', '.join(overlap)}")
        if not isinstance(self.contracts, list) or any(
            not isinstance(item, str) or not item.strip() for item in self.contracts
        ):
            problems.append("contracts must be a list of non-empty strings")
        if not isinstance(self.retirement_table, dict):
            problems.append("retirement_table must be a mapping of path to reason")
        else:
            for key, reason in self.retirement_table.items():
                if not isinstance(key, str) or not key.strip():
                    problems.append("retirement_table keys must be non-empty strings")
                if not isinstance(reason, str) or not reason.strip():
                    problems.append(f"retirement_table[{key!r}] reason must be non-empty")
        return problems

    def normalize(self) -> Lane:
        """Return a copy with canonical scope patterns."""
        return Lane(
            lane_id=self.lane_id,
            task_id=self.task_id,
            prompt=self.prompt,
            touches=_require_scope_list(self.touches, "touches"),
            must_not_touch=_require_scope_list(self.must_not_touch, "must_not_touch"),
            contracts=list(self.contracts),
            retirement_table=dict(self.retirement_table),
            worker_route=self.worker_route,
            reviewer_route=self.reviewer_route,
            effort=self.effort,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping (deterministic key order)."""
        return {
            "lane_id": self.lane_id,
            "task_id": self.task_id,
            "prompt": self.prompt,
            "touches": list(self.touches),
            "must_not_touch": list(self.must_not_touch),
            "contracts": list(self.contracts),
            "retirement_table": dict(self.retirement_table),
            "worker_route": self.worker_route,
            "reviewer_route": self.reviewer_route,
            "effort": self.effort,
        }

    @classmethod
    def from_dict(cls, payload: object) -> Lane:
        """Build one lane from a mapping, validating every field."""
        if not isinstance(payload, Mapping):
            raise WaveModelError("lane payload must be a mapping")
        return cls(
            lane_id=_require_str(payload.get("lane_id"), "lane_id"),
            task_id=_require_str(payload.get("task_id"), "task_id"),
            prompt=_require_str(payload.get("prompt"), "prompt"),
            touches=_require_str_list(payload.get("touches", []), "touches"),
            must_not_touch=_require_str_list(
                payload.get("must_not_touch", []), "must_not_touch"
            ),
            contracts=_require_str_list(payload.get("contracts", []), "contracts"),
            retirement_table=_require_str_map(
                payload.get("retirement_table", {}), "retirement_table"
            ),
            worker_route=_require_str(payload.get("worker_route"), "worker_route"),
            reviewer_route=_require_str(payload.get("reviewer_route"), "reviewer_route"),
            effort=_require_str(payload.get("effort"), "effort"),
        )


def _require_str_map(value: object, field_name: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise WaveModelError(f"{field_name} must be a mapping")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            raise WaveModelError(f"{field_name} keys must be non-empty strings")
        if not isinstance(item, str) or not item.strip():
            raise WaveModelError(f"{field_name}[{key!r}] must be a non-empty string")
        result[key] = item
    return result


@dataclass(frozen=True)
class Wave:
    """A wave: base SHA, lanes, merge order, named glue and budgets (ADR-0004 D2)."""

    wave_id: str
    base_sha: str
    lanes: list[Lane] = field(default_factory=list)
    merge_order: list[str] = field(default_factory=list)
    named_glue: list[str] = field(default_factory=list)
    budgets: dict[str, Any] = field(default_factory=dict)
    target_branch: str = ""

    def __post_init__(self) -> None:
        problems = self.problems()
        if problems:
            raise WaveModelError("; ".join(problems))

    def problems(self) -> list[str]:
        """Return deterministic structural problems; empty means valid."""
        problems: list[str] = []
        if not isinstance(self.wave_id, str) or not self.wave_id.strip():
            problems.append("wave_id must be a non-empty string")
        if not _is_sha(self.base_sha):
            problems.append("base_sha must be a full lowercase hex commit SHA")
        if not isinstance(self.target_branch, str) or not self.target_branch.strip():
            problems.append("target_branch must be a non-empty string")
        if not isinstance(self.lanes, list) or not self.lanes:
            problems.append("lanes must list at least one lane")
        else:
            seen: list[str] = []
            for lane in self.lanes:
                if not isinstance(lane, Lane):
                    problems.append("lanes must contain Lane instances")
                    continue
                for problem in lane.problems():
                    problems.append(f"lane {lane.lane_id!r}: {problem}")
                if lane.lane_id in seen:
                    problems.append(f"duplicate lane_id: {lane.lane_id}")
                seen.append(lane.lane_id)
            if isinstance(self.merge_order, list):
                if sorted(self.merge_order) != sorted(seen):
                    problems.append(
                        "merge_order must list every lane_id exactly once"
                    )
        if not isinstance(self.merge_order, list):
            problems.append("merge_order must be a list of lane ids")
        if not isinstance(self.named_glue, list) or any(
            not isinstance(item, str) or not item.strip() for item in self.named_glue
        ):
            problems.append("named_glue must be a list of non-empty strings")
        elif len(set(self.named_glue)) != len(self.named_glue):
            problems.append("named_glue must not repeat an item")
        problems.extend(self._budget_problems())
        return problems

    def _budget_problems(self) -> list[str]:
        if not isinstance(self.budgets, Mapping):
            return ["budgets must be a mapping"]
        problems: list[str] = []
        for key, value in self.budgets.items():
            if not isinstance(key, str) or not key.strip():
                problems.append("budgets keys must be non-empty strings")
                continue
            if key == "g3_runs":
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    problems.append("budgets['g3_runs'] must be a positive integer")
                elif value > MAX_G3_RUNS_PER_WAVE:
                    problems.append(
                        f"budgets['g3_runs'] may not exceed {MAX_G3_RUNS_PER_WAVE}"
                    )
            elif key == "fix_rounds":
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    problems.append("budgets['fix_rounds'] must be a non-negative integer")
                elif value > MAX_FIX_ROUNDS_PER_FAILURE:
                    problems.append(
                        f"budgets['fix_rounds'] may not exceed {MAX_FIX_ROUNDS_PER_FAILURE}"
                    )
            else:
                try:
                    json.dumps(value)
                except (TypeError, ValueError):
                    problems.append(f"budgets[{key!r}] must be JSON-serializable")
        return problems

    # -- accessors ---------------------------------------------------------

    @property
    def lane_ids(self) -> tuple[str, ...]:
        """Lane identities in declared order."""
        return tuple(lane.lane_id for lane in self.lanes)

    def lane_by_id(self, lane_id: str) -> Lane:
        """Return the lane with this id, or raise :class:`WaveModelError`."""
        for lane in self.lanes:
            if lane.lane_id == lane_id:
                return lane
        raise WaveModelError(f"unknown lane_id: {lane_id!r}")

    def budget(self, name: str, default: int) -> int:
        """Return an integer budget, defaulting when the key is absent."""
        value = self.budgets.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise WaveModelError(f"budgets[{name!r}] must be an integer")
        return value

    # -- serialization -----------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping (deterministic key order)."""
        return {
            "wave_id": self.wave_id,
            "base_sha": self.base_sha,
            "lanes": [lane.as_dict() for lane in self.lanes],
            "merge_order": list(self.merge_order),
            "named_glue": list(self.named_glue),
            "budgets": dict(self.budgets),
            "target_branch": self.target_branch,
        }

    @classmethod
    def from_dict(cls, payload: object) -> Wave:
        """Build a wave from a mapping, validating every field fail-closed."""
        if not isinstance(payload, Mapping):
            raise WaveModelError("wave payload must be a mapping")
        raw_lanes = payload.get("lanes")
        if not isinstance(raw_lanes, list):
            raise WaveModelError("lanes must be a list")
        budgets = payload.get("budgets", {})
        if not isinstance(budgets, Mapping):
            raise WaveModelError("budgets must be a mapping")
        return cls(
            wave_id=_require_str(payload.get("wave_id"), "wave_id"),
            base_sha=_require_str(payload.get("base_sha"), "base_sha"),
            lanes=[Lane.from_dict(item) for item in raw_lanes],
            merge_order=_require_str_list(payload.get("merge_order"), "merge_order"),
            named_glue=_require_str_list(payload.get("named_glue", []), "named_glue"),
            budgets=dict(budgets),
            target_branch=_require_str(payload.get("target_branch"), "target_branch"),
        )

    def to_json(self) -> str:
        """Serialize to canonical (sorted-key) JSON text."""
        return json.dumps(self.as_dict(), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> Wave:
        """Parse canonical JSON text, validating fail-closed."""
        try:
            payload = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise WaveModelError(f"wave JSON is not parseable: {exc}") from exc
        return cls.from_dict(payload)

    def as_sqlite_row(self) -> dict[str, Any]:
        """Return one SQLite-compatible row of scalar / JSON-text columns."""
        return {
            "wave_id": self.wave_id,
            "base_sha": self.base_sha,
            "target_branch": self.target_branch,
            "merge_order_json": json.dumps(list(self.merge_order)),
            "named_glue_json": json.dumps(list(self.named_glue)),
            "budgets_json": json.dumps(dict(self.budgets), sort_keys=True),
            "lanes_json": json.dumps([lane.as_dict() for lane in self.lanes]),
        }

    @classmethod
    def from_sqlite_row(cls, row: Mapping[str, Any]) -> Wave:
        """Rebuild a wave from :meth:`as_sqlite_row` output, validating types."""
        if not isinstance(row, Mapping):
            raise WaveModelError("wave row must be a mapping")
        return cls(
            wave_id=_require_str(row.get("wave_id"), "wave_id"),
            base_sha=_require_str(row.get("base_sha"), "base_sha"),
            target_branch=_require_str(row.get("target_branch"), "target_branch"),
            merge_order=_require_str_list(
                _json_list(row.get("merge_order_json"), "merge_order_json"),
                "merge_order",
            ),
            named_glue=_require_str_list(
                _json_list(row.get("named_glue_json"), "named_glue_json"),
                "named_glue",
            ),
            budgets=_json_map(row.get("budgets_json"), "budgets_json"),
            lanes=[
                Lane.from_dict(item)
                for item in _json_list(row.get("lanes_json"), "lanes_json")
            ],
        )


def _json_list(text: object, field_name: str) -> list[Any]:
    if not isinstance(text, str):
        raise WaveModelError(f"{field_name} must be JSON text")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise WaveModelError(f"{field_name} is not parseable JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise WaveModelError(f"{field_name} must decode to a list")
    return payload


def _json_map(text: object, field_name: str) -> dict[str, Any]:
    if not isinstance(text, str):
        raise WaveModelError(f"{field_name} must be JSON text")
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise WaveModelError(f"{field_name} is not parseable JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise WaveModelError(f"{field_name} must decode to a mapping")
    return payload


def wave_from_sequence(
    *, wave_id: str, base_sha: str, lanes: Iterable[Mapping[str, Any]], **rest: Any
) -> Wave:
    """Convenience constructor from raw lane mappings (validated, normalized)."""
    built = [Lane.from_dict(item).normalize() for item in lanes]
    return Wave(
        wave_id=wave_id,
        base_sha=base_sha,
        lanes=built,
        merge_order=list(rest.pop("merge_order", [lane.lane_id for lane in built])),
        named_glue=list(rest.pop("named_glue", [])),
        budgets=dict(rest.pop("budgets", {})),
        target_branch=str(rest.pop("target_branch", "")),
    )
