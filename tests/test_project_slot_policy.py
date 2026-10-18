"""Adversarial tests for the pure project-child slot admission policy.

Every test is hermetic: no database, no filesystem mutation, no provider call,
no network and no sleeping. The module under test is a pure function, so each
case constructs an immutable snapshot and request and asserts the typed
decision, its counts, its reason codes and its fail-closed behaviour.
"""

from __future__ import annotations

import dataclasses
from typing import cast

import pytest

from saberops.project_slot_policy import (
    DEFAULT_ACTIVE_PROJECT_LIMIT,
    DEFAULT_CHILD_SLOTS_PER_PROJECT_LIMIT,
    OCCUPYING_SLOT_STATES,
    AdmissionReason,
    AdmissionStatus,
    ProjectSlotRecord,
    SlotAdmissionDecision,
    SlotAdmissionRequest,
    SlotKind,
    SlotLimits,
    SlotSnapshot,
    SlotState,
    SnapshotSource,
    default_slot_limits,
    evaluate_slot_admission,
)
from saberops.wave_executor import (
    LANE_STATE_TO_SLOT_STATE,
    MergeOutcome,
    RecordingWaveEventSink,
    WaveExecutor,
)
from saberops.wave_models import Lane, LaneState, Wave

OWNER = "owner-1"


def _record(
    project_id: str,
    task_id: str,
    slot_id: str,
    kind: str = SlotKind.CHILD_TASK.value,
    state: str = SlotState.EXECUTING.value,
) -> ProjectSlotRecord:
    return ProjectSlotRecord(
        project_id=project_id, task_id=task_id, slot_id=slot_id, kind=kind, state=state
    )


def _child(
    project_id: str, index: int, state: str = SlotState.EXECUTING.value
) -> ProjectSlotRecord:
    return _record(
        project_id,
        f"{project_id}-task-{index}",
        f"{project_id}-slot-{index}",
        state=state,
    )


def _snapshot(
    slots: list[ProjectSlotRecord],
    *,
    owner_id: object = OWNER,
    source: str = SnapshotSource.OWNER_DB.value,
    complete: bool = True,
) -> SlotSnapshot:
    return SlotSnapshot(
        owner_id=cast(str, owner_id),
        source=source,
        complete=complete,
        slots=tuple(slots),
    )


def _request(
    candidate: ProjectSlotRecord,
    *,
    owner_id: object = OWNER,
    limits: SlotLimits | None = None,
) -> SlotAdmissionRequest:
    return SlotAdmissionRequest(
        owner_id=cast(str, owner_id),
        candidate=candidate,
        limits=SlotLimits() if limits is None else limits,
    )


def _decide(
    snapshot: SlotSnapshot, candidate: ProjectSlotRecord, *, limits: SlotLimits | None = None
) -> SlotAdmissionDecision:
    return evaluate_slot_admission(_request(candidate, limits=limits), snapshot)


def test_single_project_is_capped_at_five_children() -> None:
    """Covers: ADR-0004 Amendment 3 -- 1 project with 6 tasks -> fifth cap."""
    five = [_child("p1", index) for index in range(1, 6)]
    sixth = _child("p1", 6)

    decision = _decide(_snapshot(five), sixth)

    assert decision.status is AdmissionStatus.REJECTED
    assert decision.reason is AdmissionReason.PROJECT_SLOT_LIMIT_REACHED
    assert decision.admitted is False
    assert decision.counts.active_projects == 1
    assert decision.counts.occupied_child_slots == 5

    fourth_occupied = _decide(_snapshot(five[:4]), _child("p1", 5))
    assert fourth_occupied.status is AdmissionStatus.ADMITTED


def test_three_project_ceiling_rejects_fourth_project() -> None:
    """Covers: ADR-0004 Amendment 3 -- 4 projects with 1 task each -> three project cap."""
    active = [_child(f"p{index}", 1) for index in range(1, 4)]

    fourth = _decide(_snapshot(active), _child("p4", 1))
    assert fourth.status is AdmissionStatus.REJECTED
    assert fourth.reason is AdmissionReason.PROJECT_LIMIT_REACHED
    assert fourth.counts.active_projects == 3

    third = _decide(_snapshot(active[:2]), _child("p3", 1))
    assert third.status is AdmissionStatus.ADMITTED


def test_three_by_five_admits_fifteenth_and_rejects_sixteenth() -> None:
    """Covers: ADR-0004 Amendment 3 -- 3x5 upper bound is admitted; 15 is derived."""
    fourteen = [
        *[_child("p1", index) for index in range(1, 6)],
        *[_child("p2", index) for index in range(1, 6)],
        *[_child("p3", index) for index in range(1, 5)],
    ]
    admitted = _decide(_snapshot(fourteen), _child("p3", 5))
    assert admitted.status is AdmissionStatus.ADMITTED
    assert admitted.counts.active_projects == 3
    assert admitted.counts.occupied_child_slots == 14
    assert admitted.counts.derived_global_ceiling == 15
    assert admitted.counts.derived_global_ceiling == (
        DEFAULT_ACTIVE_PROJECT_LIMIT * DEFAULT_CHILD_SLOTS_PER_PROJECT_LIMIT
    )

    fifteen = [*fourteen, _child("p3", 5)]
    rejected = _decide(_snapshot(fifteen), _child("p3", 6))
    assert rejected.status is AdmissionStatus.REJECTED
    assert rejected.reason is AdmissionReason.PROJECT_SLOT_LIMIT_REACHED
    assert rejected.counts.occupied_child_slots == 15


def test_identical_duplicate_slot_records_count_once() -> None:
    """Covers: contract -- duplicate identical slot records are an alias, not a new lane."""
    distinct = [_child("p1", index) for index in range(1, 5)]
    alias = distinct[0]
    snapshot = _snapshot([*distinct, alias])
    assert snapshot.is_authoritative is True

    decision = _decide(snapshot, _child("p1", 5))

    assert decision.counts.occupied_child_slots == 4
    assert decision.status is AdmissionStatus.ADMITTED


def test_conflicting_slot_record_denies_fail_closed() -> None:
    """Covers: contract -- conflicting duplicate slot record denies new admission."""
    executing = _child("p1", 1)
    conflicting = dataclasses.replace(executing, state=SlotState.QUEUED.value)

    decision = _decide(_snapshot([executing, conflicting]), _child("p1", 2))

    assert decision.status is AdmissionStatus.DENIED
    assert decision.reason is AdmissionReason.CONFLICTING_SLOT_RECORD
    assert decision.counts_authoritative is False


def test_conflicting_project_identity_denies_fail_closed() -> None:
    """Covers: contract -- one slot id may not be observed under two project ids."""
    first = _child("p1", 1)
    other_project = dataclasses.replace(first, project_id="p2")

    decision = _decide(_snapshot([first, other_project]), _child("p3", 1))

    assert decision.status is AdmissionStatus.DENIED
    assert decision.reason is AdmissionReason.CONFLICTING_PROJECT_IDENTITY


def test_conflicting_task_identity_denies_fail_closed() -> None:
    """Covers: contract -- one child task may not be observed in two distinct slots."""
    one = _child("p1", 1)
    other_slot = dataclasses.replace(one, slot_id="p1-slot-other")

    decision = _decide(_snapshot([one, other_slot]), _child("p1", 2))

    assert decision.status is AdmissionStatus.DENIED
    assert decision.reason is AdmissionReason.CONFLICTING_TASK_IDENTITY


def test_missing_identity_denies() -> None:
    """Covers: contract -- missing/corrupt project, task or slot identity denies."""
    empty_candidate = _record("", "task", "slot")
    from_candidate = _decide(_snapshot([]), empty_candidate)
    assert from_candidate.status is AdmissionStatus.DENIED
    assert from_candidate.reason is AdmissionReason.MISSING_IDENTITY

    corrupt_row = _record("p1", "task", "   ")
    from_row = _decide(_snapshot([corrupt_row]), _child("p1", 1))
    assert from_row.status is AdmissionStatus.DENIED
    assert from_row.reason is AdmissionReason.MISSING_IDENTITY


def test_incomplete_snapshot_denies() -> None:
    """Covers: contract -- an incomplete snapshot is never treated as free capacity."""
    decision = _decide(_snapshot([], complete=False), _child("p1", 1))

    assert decision.status is AdmissionStatus.DENIED
    assert decision.reason is AdmissionReason.INCOMPLETE_SNAPSHOT
    assert decision.counts_authoritative is False


def test_unknown_snapshot_source_denies() -> None:
    """Covers: contract -- an unknown owner DB/snapshot source is not proven free."""
    explicit_unknown = _decide(_snapshot([], source=SnapshotSource.UNKNOWN.value), _child("p1", 1))
    assert explicit_unknown.status is AdmissionStatus.DENIED
    assert explicit_unknown.reason is AdmissionReason.UNKNOWN_SNAPSHOT_SOURCE

    unregistered = _decide(_snapshot([], source="local-cache"), _child("p1", 1))
    assert unregistered.status is AdmissionStatus.DENIED
    assert unregistered.reason is AdmissionReason.UNKNOWN_SNAPSHOT_SOURCE


def test_unknown_slot_state_denies() -> None:
    """Covers: contract -- an unrecognized slot state denies instead of being ignored."""
    bad_candidate = _record("p1", "task", "slot", state="running")
    from_candidate = _decide(_snapshot([]), bad_candidate)
    assert from_candidate.status is AdmissionStatus.DENIED
    assert from_candidate.reason is AdmissionReason.UNKNOWN_SLOT_STATE

    bad_row = _record("p1", "task", "slot", state="paused")
    from_row = _decide(_snapshot([bad_row]), _child("p1", 1))
    assert from_row.status is AdmissionStatus.DENIED
    assert from_row.reason is AdmissionReason.UNKNOWN_SLOT_STATE


def test_unknown_slot_kind_denies() -> None:
    """Covers: contract -- an unrecognized record kind denies instead of being counted."""
    bad_kind = _record("p1", "task", "slot", kind="phase-wizard")

    decision = _decide(_snapshot([bad_kind]), _child("p1", 1))

    assert decision.status is AdmissionStatus.DENIED
    assert decision.reason is AdmissionReason.UNKNOWN_SLOT_KIND


def test_owner_mismatch_denies() -> None:
    """Covers: contract -- a mismatched owner is not proven free and fails closed."""
    decision = _decide(_snapshot([], owner_id="owner-2"), _child("p1", 1))

    assert decision.status is AdmissionStatus.DENIED
    assert decision.reason is AdmissionReason.OWNER_MISMATCH


def test_non_positive_limits_deny() -> None:
    """Covers: contract -- limits must be explicitly positive; zero/negative deny."""
    zero_projects = _decide(_snapshot([]), _child("p1", 1), limits=SlotLimits(0, 5))
    assert zero_projects.status is AdmissionStatus.DENIED
    assert zero_projects.reason is AdmissionReason.INVALID_LIMIT

    negative_slots = _decide(_snapshot([]), _child("p1", 1), limits=SlotLimits(3, -1))
    assert negative_slots.status is AdmissionStatus.DENIED
    assert negative_slots.reason is AdmissionReason.INVALID_LIMIT

    both_zero = _decide(_snapshot([]), _child("p1", 1), limits=SlotLimits(0, 0))
    assert both_zero.reason is AdmissionReason.INVALID_LIMIT


def test_lowering_limits_denies_new_starts_without_terminating_running_tasks() -> None:
    """Covers: contract -- lowering limits parks new work but never terminates occupancy."""
    running = [_child("p1", 1), _child("p1", 2), _child("p2", 1), _child("p2", 2)]
    lowered = SlotLimits(active_projects=1, child_slots_per_project=1)

    new_project = _decide(
        _snapshot(running), _child("p3", 1), limits=lowered
    )
    assert new_project.status is AdmissionStatus.REJECTED
    assert new_project.reason is AdmissionReason.PROJECT_LIMIT_REACHED
    # Existing occupancy is reported untouched: nothing was terminated.
    assert new_project.counts.occupied_child_slots == 4
    assert new_project.counts.active_projects == 2

    existing_project = _decide(
        _snapshot(running), _child("p1", 3), limits=lowered
    )
    assert existing_project.status is AdmissionStatus.REJECTED
    assert existing_project.reason is AdmissionReason.PROJECT_SLOT_LIMIT_REACHED


def test_wrappers_and_queued_children_do_not_consume_slots() -> None:
    """Covers: contract -- wave parent plus five children counts five, not six."""
    wrapper = _record("p1", "wave", "p1-wave", kind=SlotKind.WAVE_WRAPPER.value)
    plan = _record("p1", "plan", "p1-plan", kind=SlotKind.PLAN_WRAPPER.value)
    turn = _record("p1", "turn", "p1-turn", kind=SlotKind.ORCHESTRATOR_TURN.value)
    children = [_child("p1", index) for index in range(1, 6)]
    queued = _child("p1", 7, state=SlotState.QUEUED.value)
    blocked = _child("p1", 8, state=SlotState.BLOCKED.value)
    terminal = _child("p1", 9, state=SlotState.TERMINAL.value)

    snapshot = _snapshot([wrapper, plan, turn, *children, queued, blocked, terminal])
    decision = _decide(snapshot, _child("p1", 6))

    assert decision.counts.occupied_child_slots == 5
    assert decision.counts.active_projects == 1
    assert decision.counts.excluded_records == 6
    assert decision.reason is AdmissionReason.PROJECT_SLOT_LIMIT_REACHED


def test_queued_candidate_is_admitted_without_occupying() -> None:
    """Covers: contract -- a non-occupying candidate consumes no execution slot."""
    decision = _decide(_snapshot([]), _child("p1", 1, state=SlotState.QUEUED.value))

    assert decision.status is AdmissionStatus.ADMITTED
    assert decision.counts.occupied_child_slots == 0
    assert decision.counts.active_projects == 0


def test_replay_of_an_active_slot_is_denied() -> None:
    """Covers: contract -- re-submitting an active slot is not launch authorization."""
    slot = _child("p1", 1)
    decision = _decide(_snapshot([slot]), dataclasses.replace(slot))
    assert decision.status is AdmissionStatus.DENIED
    assert decision.reason is AdmissionReason.SLOT_ALREADY_ACTIVE

    renamed_slot = dataclasses.replace(slot, slot_id="p1-slot-renamed")
    other = _decide(_snapshot([slot]), renamed_slot)
    assert other.status is AdmissionStatus.DENIED
    assert other.reason is AdmissionReason.SLOT_ALREADY_ACTIVE


def test_non_child_candidate_is_denied() -> None:
    """Covers: contract -- a wrapper or model turn cannot be admitted as a child slot."""
    wrapper = _record(
        "p1", "wave", "p1-wave", kind=SlotKind.WAVE_WRAPPER.value, state=SlotState.EXECUTING.value
    )

    decision = _decide(_snapshot([]), wrapper)

    assert decision.status is AdmissionStatus.DENIED
    assert decision.reason is AdmissionReason.CANDIDATE_NOT_CHILD_TASK


def test_derived_ceiling_is_not_a_standalone_limit() -> None:
    """Covers: ADR-0004 Amendment 3 -- derived 15 is not a replacement for 3x5."""
    # Nine occupied slots (three projects x three) is below a custom derived
    # ceiling of ten, yet the third active project already blocks a fourth.
    occupied = [
        *[_child("p1", index) for index in range(1, 4)],
        *[_child("p2", index) for index in range(1, 4)],
        *[_child("p3", index) for index in range(1, 4)],
    ]
    decision = _decide(
        _snapshot(occupied),
        _child("p4", 1),
        limits=SlotLimits(active_projects=3, child_slots_per_project=3),
    )

    assert decision.counts.occupied_child_slots == 9
    assert decision.counts.derived_global_ceiling == 9
    assert decision.status is AdmissionStatus.REJECTED
    assert decision.reason is AdmissionReason.PROJECT_LIMIT_REACHED


def test_decisions_are_deterministic_and_reasons_are_stably_ordered() -> None:
    """Covers: contract -- deterministic decision, counts and ordered reason codes."""
    snapshot = _snapshot(
        [_child("p1", index) for index in range(1, 6)],
        owner_id="owner-2",
        source=SnapshotSource.UNKNOWN.value,
        complete=False,
    )
    candidate = _child("p1", 6)

    first = _decide(snapshot, candidate)
    second = _decide(snapshot, candidate)
    assert first == second

    # Reasons appear in fixed enum precedence order, not set-iteration order.
    assert first.reason_codes() == (
        AdmissionReason.UNKNOWN_SNAPSHOT_SOURCE.value,
        AdmissionReason.INCOMPLETE_SNAPSHOT.value,
        AdmissionReason.OWNER_MISMATCH.value,
    )
    assert first.status is AdmissionStatus.DENIED
    assert first.reason is AdmissionReason.UNKNOWN_SNAPSHOT_SOURCE


def test_snapshot_row_order_does_not_change_the_decision() -> None:
    """Covers: contract -- phase/ordinal order carries no scheduling meaning."""
    forward = [_child(f"p{index}", 1) for index in range(1, 4)]
    reversed_rows = list(reversed(forward))

    one = _decide(_snapshot(forward), _child("p4", 1))
    two = _decide(_snapshot(reversed_rows), _child("p4", 1))

    assert one == two
    assert one.counts.occupied_child_slots_by_project == (
        ("p1", 1),
        ("p2", 1),
        ("p3", 1),
    )


def test_inputs_are_immutable_and_unchanged_by_evaluation() -> None:
    """Covers: contract -- immutable typed inputs; evaluation mutates nothing."""
    slots = tuple(_child("p1", index) for index in range(1, 3))
    snapshot = _snapshot(list(slots))
    candidate = _child("p1", 3)
    request = _request(candidate)

    _ = evaluate_slot_admission(request, snapshot)

    assert snapshot.slots == slots
    assert request.candidate == candidate
    assert request.limits == default_slot_limits()

    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.slot_id = "mutated"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.complete = False  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.limits = SlotLimits(1, 1)  # type: ignore[misc]


def test_default_limits_are_three_projects_and_five_tasks() -> None:
    """Covers: ADR-0004 Amendment 3 -- owner default 3 active projects x 5 tasks."""
    limits = default_slot_limits()
    assert limits.active_projects == DEFAULT_ACTIVE_PROJECT_LIMIT == 3
    assert limits.child_slots_per_project == DEFAULT_CHILD_SLOTS_PER_PROJECT_LIMIT == 5
    assert limits.derived_global_ceiling == 15


def test_empty_snapshot_admits_the_first_child() -> None:
    """Covers: contract -- a valid complete empty snapshot admits the first child."""
    decision = _decide(_snapshot([]), _child("p1", 1))

    assert decision.status is AdmissionStatus.ADMITTED
    assert decision.admitted is True
    assert decision.reason is AdmissionReason.ADMITTED
    assert decision.counts_authoritative is True
    assert decision.counts.active_projects == 0
    assert decision.counts.occupied_child_slots == 0


def test_blank_or_unknown_owner_identity_never_admits() -> None:
    """Covers: contract -- unknown/blank owner on either side denies fail-closed.

    Adversarial: with an otherwise valid, complete, empty snapshot and valid
    limits, two blank owners (``""``/``None``/whitespace) previously matched and
    could reach ADMITTED. A blank owner must never be a matching identity.
    """
    candidate = _child("p1", 1)
    for unknown in ("", "   ", None):
        from_snapshot = evaluate_slot_admission(
            _request(candidate), _snapshot([], owner_id=unknown)
        )
        assert from_snapshot.status is AdmissionStatus.DENIED
        assert from_snapshot.reason is AdmissionReason.MISSING_IDENTITY
        assert from_snapshot.counts_authoritative is False
        assert from_snapshot.admitted is False

        from_request = evaluate_slot_admission(
            _request(candidate, owner_id=unknown), _snapshot([])
        )
        assert from_request.status is AdmissionStatus.DENIED
        assert from_request.reason is AdmissionReason.MISSING_IDENTITY
        assert from_request.counts_authoritative is False

        both_unknown = evaluate_slot_admission(
            _request(candidate, owner_id=unknown), _snapshot([], owner_id=unknown)
        )
        assert both_unknown.status is AdmissionStatus.DENIED
        assert both_unknown.reason is AdmissionReason.MISSING_IDENTITY
        assert both_unknown.admitted is False


def test_conflicting_known_owner_ids_deny() -> None:
    """Covers: contract -- two distinct known owners are not the same identity."""
    decision = evaluate_slot_admission(
        _request(_child("p1", 1), owner_id="owner-2"),
        _snapshot([], owner_id="owner-1"),
    )

    assert decision.status is AdmissionStatus.DENIED
    assert decision.reason is AdmissionReason.OWNER_MISMATCH
    assert decision.counts_authoritative is False
    assert decision.admitted is False

    # Trimming is shared on both sides, so padded forms of one owner still match.
    padded = evaluate_slot_admission(
        _request(_child("p1", 1), owner_id="  owner-1  "),
        _snapshot([], owner_id="owner-1"),
    )
    assert padded.status is AdmissionStatus.ADMITTED
    assert padded.reason is AdmissionReason.ADMITTED


def test_malformed_limits_deny_without_raising_or_unsafe_counts() -> None:
    """Covers: contract -- non-int/zero/negative limits deny typed, never raise."""
    candidate = _child("p1", 1)
    malformed = [
        SlotLimits(cast(int, None), 5),
        SlotLimits(3, cast(int, None)),
        SlotLimits(cast(int, "3"), 5),
        SlotLimits(cast(int, True), 5),
        SlotLimits(cast(int, 5.5), 5),
        SlotLimits(0, 5),
        SlotLimits(3, -1),
    ]
    for limits in malformed:
        # Must not raise out of derived_global_ceiling / _counts.
        decision = _decide(_snapshot([]), candidate, limits=limits)
        assert decision.status is AdmissionStatus.DENIED
        assert decision.reason is AdmissionReason.INVALID_LIMIT
        assert decision.counts_authoritative is False
        assert decision.admitted is False
        # Reported counts are non-negative and never an unsafe positive cap.
        assert decision.counts.derived_global_ceiling == 0
        assert decision.counts.project_limit >= 0
        assert decision.counts.child_slots_per_project_limit >= 0

    # The reported-only helper is itself exception-free for malformed limits.
    assert SlotLimits(cast(int, None), 5).derived_global_ceiling == 0
    assert SlotLimits(cast(int, "x"), 5).derived_global_ceiling == 0


def test_non_occupying_candidate_is_not_launch_permission() -> None:
    """Covers: contract -- queued/blocked/terminal candidate must not spawn a child.

    A non-occupying child candidate keeps its *descriptive* status ``ADMITTED``
    with reason ``non_occupying_candidate``, but both launch authorities are
    false: the plain ``admitted`` flag and the explicit ``occupies_slot``
    discriminator. The launch gate below is deliberately the naive one-liner an
    execution-slot caller would write -- ``return decision.admitted`` -- and it
    must refuse to start a non-occupying child.
    """

    def naive_launch(decision: SlotAdmissionDecision) -> bool:
        # The obvious, minimal launch gate: keyed on `admitted` alone.
        return decision.admitted

    for state in (
        SlotState.QUEUED.value,
        SlotState.BLOCKED.value,
        SlotState.TERMINAL.value,
    ):
        decision = _decide(_snapshot([]), _child("p1", 1, state=state))
        assert decision.status is AdmissionStatus.ADMITTED
        assert decision.reason is AdmissionReason.NON_OCCUPYING_CANDIDATE
        assert decision.admitted is False
        assert decision.occupies_slot is False
        assert naive_launch(decision) is False
        assert decision.counts.occupied_child_slots == 0

    # Only a genuinely slot-occupying child is launch authority.
    for state in (SlotState.RESERVED.value, SlotState.EXECUTING.value):
        occupying = _decide(_snapshot([]), _child("p1", 1, state=state))
        assert occupying.status is AdmissionStatus.ADMITTED
        assert occupying.reason is AdmissionReason.ADMITTED
        assert occupying.admitted is True
        assert occupying.occupies_slot is True
        assert naive_launch(occupying) is True


def test_admitted_is_launch_authority_for_every_non_launch_outcome() -> None:
    """Covers: contract -- `admitted` is launch authority, false for every refusal.

    Adversarial: ``admitted`` must not be a mere restatement of the descriptive
    status. A caller that gates only on ``if decision.admitted: spawn()`` must
    refuse to start a child for every non-occupying candidate, structural
    denial, limit rejection, replay or wrong-kind candidate. It is True only for
    a valid slot-occupying admission.
    """
    candidate = _child("p1", 1)

    def naive_gate(decision: SlotAdmissionDecision) -> bool:
        return decision.admitted

    non_launch: list[SlotAdmissionDecision] = [
        # Non-occupying candidates: descriptive status stays ADMITTED.
        *[
            _decide(_snapshot([]), _child("p1", 1, state=state))
            for state in (
                SlotState.QUEUED.value,
                SlotState.BLOCKED.value,
                SlotState.TERMINAL.value,
            )
        ],
        # Structural denials.
        _decide(_snapshot([], owner_id="owner-2"), candidate),
        _decide(_snapshot([], complete=False), candidate),
        _decide(_snapshot([], source=SnapshotSource.UNKNOWN.value), candidate),
        _decide(_snapshot([]), candidate, limits=SlotLimits(0, 5)),
        _decide(_snapshot([]), _record("", "task", "slot")),
        # Known-limit rejections.
        _decide(_snapshot([_child("p1", index) for index in range(1, 6)]), _child("p1", 6)),
        _decide(_snapshot([_child(f"p{index}", 1) for index in range(1, 4)]), _child("p4", 1)),
        # Replay of an active slot and a non-child candidate.
        _decide(_snapshot([candidate]), dataclasses.replace(candidate)),
        _decide(
            _snapshot([]),
            _record("p1", "wave", "p1-wave", kind=SlotKind.WAVE_WRAPPER.value),
        ),
    ]

    for decision in non_launch:
        assert decision.admitted is False
        assert decision.occupies_slot is False
        assert naive_gate(decision) is False
        # Two independent spellings of the same launch authority never diverge.
        assert decision.admitted == decision.occupies_slot

    launchable = _decide(_snapshot([]), candidate)
    assert launchable.status is AdmissionStatus.ADMITTED
    assert launchable.reason is AdmissionReason.ADMITTED
    assert launchable.admitted is True
    assert naive_gate(launchable) is True


def test_non_slot_limits_object_denies_without_raising() -> None:
    """Covers: contract -- an untyped non-SlotLimits `limits` object fails closed.

    Adversarial: ``SlotAdmissionRequest(limits=None)`` previously dereferenced
    ``limits.active_projects`` and raised ``AttributeError``. It must instead
    return the typed structural denial with non-authoritative zeroed counts and
    no unsafe positive cap (``mypy --strict`` blocks typed callers; this closes
    the untyped future-glue shape).
    """
    request = SlotAdmissionRequest(
        owner_id=OWNER,
        candidate=_child("p1", 1),
        limits=cast(SlotLimits, None),
    )

    decision = evaluate_slot_admission(request, _snapshot([]))

    assert decision.status is AdmissionStatus.DENIED
    assert decision.reason is AdmissionReason.INVALID_LIMIT
    assert decision.counts_authoritative is False
    assert decision.admitted is False
    assert decision.counts.derived_global_ceiling == 0
    assert decision.counts.project_limit == 0
    assert decision.counts.child_slots_per_project_limit == 0


# ---------------------------------------------------------------------------
# Wave-executor wiring (Step 010 / WX-1)
#
# The wave executor derives its slot records from the wave/lane model. These
# tests pin the cross-module contract with the pure policy: which lane states
# occupy a child slot, and that a wave wrapper plus N running children counts as
# N children, never N+1.
# ---------------------------------------------------------------------------


class _NoopGit:
    """Minimal eligible WaveGitPort: the admission tests never merge."""

    def resolve_ref(self, repo: str, ref: str) -> str | None:
        return None

    def is_ancestor(self, repo: str, ancestor_sha: str, descendant_sha: str) -> bool:
        return True

    def allocate_worktree(self, repo: str, branch: str, path: str, start_point: str) -> str:
        return path

    def merge_no_ff(self, worktree: str, sha: str) -> MergeOutcome:
        return MergeOutcome(ok=True)

    def head_sha(self, worktree: str) -> str:
        return "f" * 40


def _executor(lane_count: int) -> WaveExecutor:
    lanes = [
        Lane(
            lane_id=f"L{index}",
            task_id=f"task-{index}",
            prompt="work",
            touches=[f"src/{index}.py"],
            must_not_touch=[],
            contracts=[],
            retirement_table={},
            worker_route="worker-chain",
            reviewer_route="reviewer-chain",
            effort="medium",
        )
        for index in range(1, lane_count + 1)
    ]
    wave = Wave(
        wave_id="wave-1",
        base_sha="a" * 40,
        lanes=lanes,
        merge_order=[lane.lane_id for lane in lanes],
        named_glue=[],
        budgets={},
        target_branch="main",
    )
    return WaveExecutor(
        wave,
        git=_NoopGit(),
        event_sink=RecordingWaveEventSink(),
        repo="/repo",
        worktree_base="/worktrees",
        project_id="proj-A",
        integration_worktree="/worktrees/wave-1/integration",
    )


def test_lane_state_occupancy_mapping_agrees_with_the_policy() -> None:
    """Covers: ADR-0004 Amendment 3 + prompt contract 5 (3x5 occupancy mapping)."""
    assert set(LANE_STATE_TO_SLOT_STATE) == set(LaneState)
    occupying = {
        lane_state
        for lane_state, slot_state in LANE_STATE_TO_SLOT_STATE.items()
        if slot_state in OCCUPYING_SLOT_STATES
    }
    assert occupying == {LaneState.RUNNING, LaneState.FIX_UP_K}
    assert all(
        isinstance(slot_state, SlotState) for slot_state in LANE_STATE_TO_SLOT_STATE.values()
    )


def test_wave_records_use_policy_kinds_and_never_double_count() -> None:
    """Covers: ADR-0004 Amendment 3 + prompt contract 5 (wrappers take no slot)."""
    executor = _executor(5)
    executor.start()
    for lane_id in ("L1", "L2", "L3", "L4"):
        executor.start_lane(lane_id)
    records = executor.slot_records("proj-A")
    assert records[0].kind == SlotKind.WAVE_WRAPPER.value
    assert records[0].state not in {state.value for state in OCCUPYING_SLOT_STATES}
    assert [record.kind for record in records[1:]] == [SlotKind.CHILD_TASK.value] * 5
    # The wave wrapper is not a child task, so four running children plus the
    # wrapper still admit a fifth child.
    decision = evaluate_slot_admission(
        _request(_child("proj-A", 5)),
        SlotSnapshot(
            owner_id=OWNER,
            source=SnapshotSource.OWNER_DB.value,
            complete=True,
            slots=tuple(records),
        ),
    )
    assert decision.admitted is True
    assert decision.counts.occupied_child_slots == 4
    assert decision.counts.active_projects == 1


def test_derived_wave_snapshot_enforces_the_fifth_child_boundary() -> None:
    """Covers: ADR-0004 Amendment 3 (1x6 boundary through the wave executor)."""
    executor = _executor(6)
    executor.start()
    for lane_id in ("L1", "L2", "L3", "L4", "L5"):
        executor.start_lane(lane_id)
    decision = executor.admit_child_lane("L6", owner_id="owner-1")
    assert decision.status is AdmissionStatus.REJECTED
    assert decision.reason is AdmissionReason.PROJECT_SLOT_LIMIT_REACHED
    assert decision.counts.occupied_child_slots == 5
