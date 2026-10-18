"""Wave and lane model construction, validation, state machines and serialization.

Every test is hermetic: no git, no database, no filesystem mutation, no
provider call. The models under test are pure immutable values.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from saberops.wave_models import (
    LANE_ALLOWED_TRANSITIONS,
    MAX_FIX_ROUNDS_PER_FAILURE,
    MAX_G3_RUNS_PER_WAVE,
    WAVE_ALLOWED_TRANSITIONS,
    Lane,
    LaneState,
    Wave,
    WaveModelError,
    WaveState,
    can_transition_lane,
    can_transition_wave,
    is_terminal_lane_state,
    is_terminal_wave_state,
    transition_lane,
    transition_wave,
    wave_from_sequence,
)

SHA_A = "a" * 40
SHA_B = "b" * 40


def _lane_from(overrides: dict[str, object]) -> Lane:
    base: dict[str, object] = {
        "lane_id": "L1",
        "task_id": "task-L1",
        "prompt": "implement the slice",
        "touches": ["src/saberops/a.py"],
        "must_not_touch": ["docs"],
        "contracts": ["wave-contract"],
        "retirement_table": {},
        "worker_route": "chain-worker",
        "reviewer_route": "chain-reviewer",
        "effort": "medium",
    }
    base.update(overrides)
    if not base.get("task_id") or str(base.get("task_id")).startswith("task-L1"):
        base["task_id"] = f"task-{base.get('lane_id', 'L1')}"
    return Lane(**base)  # type: ignore[arg-type]


def _lane(lane_id: str = "L1", touches: list[str] | None = None, **overrides: object) -> Lane:
    fields: dict[str, object] = {"lane_id": lane_id}
    if touches is not None:
        fields["touches"] = touches
    fields.update(overrides)
    return _lane_from(fields)


def _wave(**overrides: object) -> Wave:
    lane = _lane()
    base: dict[str, object] = {
        "wave_id": "wave-1",
        "base_sha": SHA_A,
        "lanes": [lane],
        "merge_order": [lane.lane_id],
        "named_glue": [],
        "budgets": {},
        "target_branch": "main",
    }
    base.update(overrides)
    return Wave(**base)  # type: ignore[arg-type]


# -- construction -----------------------------------------------------------


def test_valid_lane_and_wave_construct() -> None:
    """Covers: ADR-0004 D2 (lane/wave fields)."""
    lane = _lane()
    assert lane.lane_id == "L1"
    assert lane.touches == ["src/saberops/a.py"]
    wave = _wave()
    assert wave.lane_ids == ("L1",)
    assert wave.target_branch == "main"
    assert wave.budget("g3_runs", MAX_G3_RUNS_PER_WAVE) == MAX_G3_RUNS_PER_WAVE


@pytest.mark.parametrize(
    "overrides",
    [
        {"lane_id": ""},
        {"task_id": "  "},
        {"prompt": ""},
        {"worker_route": ""},
        {"reviewer_route": ""},
        {"effort": ""},
        {"touches": []},
        {"touches": ["src/a", "src/a"]},
        {"touches": ["/etc/passwd"]},
        {"touches": ["src/a"], "must_not_touch": ["src/a"]},
        {"contracts": [""]},
        {"retirement_table": {"tests/test_x.py": ""}},
    ],
)
def test_invalid_lane_is_refused(overrides: dict[str, object]) -> None:
    """Covers: ADR-0004 D2 (lane validation fails closed)."""
    with pytest.raises(WaveModelError):
        _lane_from(overrides)


def test_touches_escaping_the_repository_are_refused() -> None:
    """Covers: ADR-0004 D2 (repo-relative write scope)."""
    with pytest.raises(WaveModelError):
        _lane(touches=["../outside"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"wave_id": ""},
        {"base_sha": "not-a-sha"},
        {"base_sha": "A" * 40},
        {"lanes": []},
        {"target_branch": ""},
        {"merge_order": ["L1", "L2"]},
        {"merge_order": []},
        {"named_glue": [""]},
        {"named_glue": ["glue", "glue"]},
    ],
)
def test_invalid_wave_is_refused(overrides: dict[str, object]) -> None:
    """Covers: ADR-0004 D2 (wave validation fails closed)."""
    with pytest.raises(WaveModelError):
        _wave(**overrides)


def test_duplicate_lane_ids_are_refused() -> None:
    """Covers: ADR-0004 D2 (merge_order is a permutation of the lane ids)."""
    with pytest.raises(WaveModelError):
        _wave(lanes=[_lane(), _lane()], merge_order=["L1", "L1"])


def test_merge_order_must_cover_every_lane_exactly_once() -> None:
    """Covers: ADR-0004 D2 + §4 invariant 3 (deterministic merge order)."""
    lanes = [_lane("L1"), _lane("L2")]
    with pytest.raises(WaveModelError):
        _wave(lanes=lanes, merge_order=["L1"])
    wave = _wave(lanes=lanes, merge_order=["L2", "L1"])
    assert wave.merge_order == ["L2", "L1"]


def test_budget_bounds_are_enforced() -> None:
    """Covers: ADR-0004 D4 + §4 invariant 10 (budgets)."""
    assert _wave(budgets={"g3_runs": MAX_G3_RUNS_PER_WAVE}).budgets["g3_runs"] == 4
    assert _wave(budgets={"fix_rounds": MAX_FIX_ROUNDS_PER_FAILURE}).budgets["fix_rounds"] == 2
    with pytest.raises(WaveModelError):
        _wave(budgets={"g3_runs": MAX_G3_RUNS_PER_WAVE + 1})
    with pytest.raises(WaveModelError):
        _wave(budgets={"fix_rounds": MAX_FIX_ROUNDS_PER_FAILURE + 1})
    with pytest.raises(WaveModelError):
        _wave(budgets={"g3_runs": 0})
    with pytest.raises(WaveModelError):
        _wave(budgets={"g3_runs": True})


def test_budget_values_must_be_json_serializable() -> None:
    """Covers: ADR-0004 D2 (SQLite/JSON-compatible budgets)."""

    class _Opaque:
        pass

    with pytest.raises(WaveModelError):
        _wave(budgets={"custom": _Opaque()})


def test_lane_by_id_rejects_unknown_ids() -> None:
    """Covers: ADR-0004 D2 (lane identity)."""
    wave = _wave()
    assert wave.lane_by_id("L1").lane_id == "L1"
    with pytest.raises(WaveModelError):
        wave.lane_by_id("missing")


# -- state machines ---------------------------------------------------------


def test_wave_happy_path_is_the_adr_sequence() -> None:
    """Covers: ADR-0004 D2 (PLANNED..RUNNING..INTEGRATING..GATED..ACCEPTED)."""
    state = WaveState.PLANNED
    for target in (
        WaveState.RUNNING,
        WaveState.INTEGRATING,
        WaveState.GATED,
        WaveState.AWAITING_ACCEPT,
        WaveState.ACCEPTED,
    ):
        state = transition_wave(state, target)
    assert state is WaveState.ACCEPTED
    assert is_terminal_wave_state(state)


def test_wave_illegal_transitions_raise() -> None:
    """Covers: ADR-0004 D2 (fail-closed wave state machine)."""
    with pytest.raises(WaveModelError):
        transition_wave(WaveState.PLANNED, WaveState.ACCEPTED)
    with pytest.raises(WaveModelError):
        transition_wave(WaveState.RUNNING, WaveState.GATED)
    with pytest.raises(WaveModelError):
        transition_wave(WaveState.ACCEPTED, WaveState.RUNNING)
    with pytest.raises(WaveModelError):
        transition_wave("NOT_A_STATE", WaveState.RUNNING)
    assert not can_transition_wave(WaveState.GATED, WaveState.ACCEPTED)
    assert can_transition_wave(WaveState.GATED, WaveState.AWAITING_ACCEPT)


def test_wave_blocked_is_transient_and_cancelled_is_terminal() -> None:
    """Covers: ADR-0004 D2 (BLOCKED resumes; CANCELLED is terminal)."""
    assert transition_wave(WaveState.RUNNING, WaveState.BLOCKED) is WaveState.BLOCKED
    assert transition_wave(WaveState.BLOCKED, WaveState.RUNNING) is WaveState.RUNNING
    assert transition_wave(WaveState.BLOCKED, WaveState.CANCELLED) is WaveState.CANCELLED
    assert is_terminal_wave_state(WaveState.CANCELLED)
    assert not can_transition_wave(WaveState.CANCELLED, WaveState.BLOCKED)


def test_lane_happy_path_and_fix_up_round_trip() -> None:
    """Covers: ADR-0004 D2 (lane states mirror the BOARD)."""
    state = transition_lane(LaneState.PLANNED, LaneState.RUNNING)
    state = transition_lane(state, LaneState.REVIEW)
    state = transition_lane(state, LaneState.FIX_UP_K)
    state = transition_lane(state, LaneState.RUNNING)
    state = transition_lane(state, LaneState.REVIEW)
    state = transition_lane(state, LaneState.REVIEWED_PROVISIONAL)
    state = transition_lane(state, LaneState.ACCEPTED)
    assert state is LaneState.ACCEPTED
    assert is_terminal_lane_state(state)


def test_lane_illegal_transitions_raise() -> None:
    """Covers: ADR-0004 D2 (fail-closed lane state machine)."""
    with pytest.raises(WaveModelError):
        transition_lane(LaneState.REVIEW, LaneState.ACCEPTED)
    with pytest.raises(WaveModelError):
        transition_lane(LaneState.ACCEPTED, LaneState.RUNNING)
    with pytest.raises(WaveModelError):
        transition_lane("NOPE", LaneState.RUNNING)
    assert not can_transition_lane(LaneState.PLANNED, LaneState.REVIEW)


def test_transition_tables_cover_every_state() -> None:
    """Covers: ADR-0004 D2 (total transition tables)."""
    assert set(WAVE_ALLOWED_TRANSITIONS) == set(WaveState)
    assert set(LANE_ALLOWED_TRANSITIONS) == set(LaneState)
    for wave_source, wave_targets in WAVE_ALLOWED_TRANSITIONS.items():
        assert all(isinstance(target, WaveState) for target in wave_targets), wave_source
    for lane_source, lane_targets in LANE_ALLOWED_TRANSITIONS.items():
        assert all(isinstance(target, LaneState) for target in lane_targets), lane_source


# -- serialization ----------------------------------------------------------


def test_json_round_trip_is_lossless_and_canonical() -> None:
    """Covers: ADR-0004 D2 (JSON serialization)."""
    wave = _wave(named_glue=["src/saberops/glue.py"], budgets={"g3_runs": 4})
    restored = Wave.from_json(wave.to_json())
    assert restored == wave
    assert wave.to_json() == Wave.from_json(wave.to_json()).to_json()
    payload = json.loads(wave.to_json())
    assert payload["lanes"][0]["lane_id"] == "L1"


def test_sqlite_row_round_trip_is_lossless() -> None:
    """Covers: ADR-0004 D2 (SQLite-compatible dictionary)."""
    wave = _wave(named_glue=["glue.py"], budgets={"fix_rounds": 2})
    row = wave.as_sqlite_row()
    assert set(row) == {
        "wave_id",
        "base_sha",
        "target_branch",
        "merge_order_json",
        "named_glue_json",
        "budgets_json",
        "lanes_json",
    }
    assert all(isinstance(value, str) for value in row.values())
    assert Wave.from_sqlite_row(row) == wave


def test_from_dict_fails_closed_on_bad_types() -> None:
    """Covers: ADR-0004 D2 (deserialization validates)."""
    with pytest.raises(WaveModelError):
        Wave.from_dict("not a mapping")
    with pytest.raises(WaveModelError):
        Wave.from_dict({"wave_id": "w", "lanes": "nope"})
    with pytest.raises(WaveModelError):
        Wave.from_dict(
            {
                "wave_id": "w",
                "base_sha": SHA_A,
                "target_branch": "main",
                "lanes": [],
                "merge_order": [],
            }
        )
    with pytest.raises(WaveModelError):
        Lane.from_dict({"lane_id": "L1"})


def test_from_sqlite_row_fails_closed_on_bad_json() -> None:
    """Covers: ADR-0004 D2 (SQLite row validation)."""
    row = _wave().as_sqlite_row()
    row["lanes_json"] = "{not json"
    with pytest.raises(WaveModelError):
        Wave.from_sqlite_row(row)
    row = _wave().as_sqlite_row()
    row["budgets_json"] = "[1, 2]"
    with pytest.raises(WaveModelError):
        Wave.from_sqlite_row(row)


def test_from_json_rejects_unparseable_text() -> None:
    """Covers: ADR-0004 D2 (JSON parse is fail-closed)."""
    with pytest.raises(WaveModelError):
        Wave.from_json("{oops")


def test_wave_from_sequence_normalizes_scopes_and_defaults_merge_order() -> None:
    """Covers: ADR-0004 D2 (convenience constructor normalizes)."""
    wave = wave_from_sequence(
        wave_id="w-seq",
        base_sha=SHA_B,
        lanes=[
            {
                "lane_id": "L1",
                "task_id": "t1",
                "prompt": "p",
                "touches": ["./src//a.py"],
                "worker_route": "w",
                "reviewer_route": "r",
                "effort": "low",
            }
        ],
        target_branch="trunk",
    )
    assert wave.lanes[0].touches == ["src/a.py"]
    assert wave.merge_order == ["L1"]
    assert wave.target_branch == "trunk"


def test_models_are_immutable() -> None:
    """Covers: ADR-0004 D2 (immutable models)."""
    lane = _lane()
    with pytest.raises(dataclasses.FrozenInstanceError):
        lane.lane_id = "other"  # type: ignore[misc]
    wave = _wave()
    with pytest.raises(dataclasses.FrozenInstanceError):
        wave.wave_id = "other"  # type: ignore[misc]


def test_identical_inputs_produce_identical_models() -> None:
    """Covers: ADR-0004 D2 (deterministic construction)."""
    assert _wave() == _wave()
    assert _wave().to_json() == _wave().to_json()
