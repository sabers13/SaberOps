"""Adversarial contract tests for the unattended plan policy (ADR-0004 D14, RD-1).

Every test states what it protects with a ``Covers:`` docstring line. The whole
file is hermetic: no provider, CLI, login, network, database, filesystem
mutation, sleeping or engine launch. The module under test is pure, so every
instant and counter is supplied by the test itself.
"""

from __future__ import annotations

import dataclasses
from collections.abc import MutableMapping
from typing import Any, cast

import pytest

from saberops.unattended import (
    CONSECUTIVE_FAILURE_LIMIT,
    MAX_ALARM_MESSAGE_LENGTH,
    NEVER_DELEGABLE_KINDS,
    NEVER_DELEGABLE_POLICY,
    PREFLIGHT_WARN_PERCENT,
    STALL_CRITICAL_MULTIPLIER,
    UNATTENDED_PLANS_VERSION,
    AcceptedCandidateRecord,
    AlarmSeverity,
    AuthorityMode,
    AutonomousAuditEntry,
    CandidateAcceptance,
    Decision,
    DecisionClass,
    DecisionDisposition,
    DecisionResolution,
    DecisionScope,
    DecisionSheet,
    DecisionSheetError,
    MorningReport,
    NeverDelegableKind,
    NotificationChannel,
    PlanImpact,
    PlanTaskState,
    PreflightBlockerCode,
    PreflightRequest,
    PreflightResult,
    RunProgress,
    StallAlarm,
    StallObservation,
    StallSupervisionError,
    StandingDecision,
    UnattendedError,
    build_audit_entries,
    build_morning_report,
    compute_plan_impact,
    detect_stall,
    is_never_delegable,
    never_delegable_description,
    resolve_decision,
    run_preflight,
)

NOW = "2026-10-18T06:00:00+00:00"
LATER = "2026-10-18T12:00:00+00:00"
HEAD_A = "a" * 40
HEAD_B = "b" * 40
PROJECT = "proj-1"
TASK = "task-1"
KEY = "retire_tests"


def _scope(
    project: str = PROJECT,
    task: str = TASK,
    paths: tuple[str, ...] = ("src/a.py",),
) -> DecisionScope:
    return DecisionScope(project_id=project, task_id=task, paths=paths)


def _acceptance(**overrides: Any) -> CandidateAcceptance:
    fields: dict[str, Any] = {
        "candidate_id": "cand-1",
        "head_sha": HEAD_A,
        "gate_passed": True,
        "cold_review_passed": True,
        "checklist_green": True,
        "scope_authorized": True,
    }
    fields.update(overrides)
    return CandidateAcceptance(**fields)


def _decision(**overrides: Any) -> Decision:
    fields: dict[str, Any] = {
        "decision_id": "d-1",
        "scope": _scope(),
        "decision_key": KEY,
        "question": "May wave 3 rewrite these tests?",
        "evidence_paths": ("evidence/g1.txt",),
    }
    fields.update(overrides)
    return Decision(**fields)


def _standing(**overrides: Any) -> StandingDecision:
    fields: dict[str, Any] = {
        "standing_decision_id": "sd-1",
        "scope": _scope(),
        "decision_key": KEY,
        "owner_words": "Yes, wave 3 may rewrite these tests.",
        "answered_by": "owner",
        "expires_at": LATER,
    }
    fields.update(overrides)
    return StandingDecision(**fields)


def _sheet(*standing: StandingDecision, run_id: str = "run-7") -> DecisionSheet:
    return DecisionSheet(
        run_id=run_id,
        plan_reference="plan-1 wave 3",
        standing_decisions=standing,
    )


def _preflight(**overrides: Any) -> PreflightRequest:
    fields: dict[str, Any] = {
        "run_id": "run-7",
        "plan_reference": "plan-1 wave 3",
        "decision_sheet": _sheet(),
        "budget_limit_units": 100,
        "budget_spent_units": 10,
        "wall_clock_limit_seconds": 3600,
        "wall_clock_elapsed_seconds": 60,
        "authorized_paths": ("src/a.py", "tests/test_a.py"),
        "worker_cap": 2,
        "active_workers": 1,
        "notification_channels": (NotificationChannel.LOG,),
    }
    fields.update(overrides)
    return PreflightRequest(**fields)


# --------------------------------------------------------------------------
# Never-delegable list: immutability and the exact frozen categories
# --------------------------------------------------------------------------


def test_never_delegable_list_covers_the_seven_categories() -> None:
    """Covers: RD-1 requirement 2 never-delegable categories."""
    assert set(NEVER_DELEGABLE_KINDS) == {
        NeverDelegableKind.TEST_GATE,
        NeverDelegableKind.DATA_TRAINING_POLICY,
        NeverDelegableKind.ROADMAP_SCOPE,
        NeverDelegableKind.BUDGET_CAP,
        NeverDelegableKind.CREDENTIALS,
        NeverDelegableKind.DESTRUCTIVE_OPERATIONS,
        NeverDelegableKind.PUBLISHING,
    }


def test_never_delegable_list_is_immutable_data() -> None:
    """Covers: RD-1 requirement 2 the list is enforced in software, not a comment."""
    assert isinstance(NEVER_DELEGABLE_KINDS, tuple)
    assert not hasattr(NEVER_DELEGABLE_KINDS, "append")
    mutable = cast("MutableMapping[NeverDelegableKind, str]", NEVER_DELEGABLE_POLICY)
    with pytest.raises(TypeError):
        mutable[NeverDelegableKind.TEST_GATE] = "widened"
    assert never_delegable_description(NeverDelegableKind.TEST_GATE).startswith("disabling")


@pytest.mark.parametrize("kind", list(NeverDelegableKind))
def test_every_kind_is_never_delegable(kind: NeverDelegableKind) -> None:
    """Covers: RD-1 requirement 2 every listed kind is non-delegable."""
    assert is_never_delegable(kind) is True
    assert never_delegable_description(kind)


def test_is_never_delegable_rejects_unknown_kind() -> None:
    """Covers: RD-1 requirement 2 fail closed on an unknown kind."""
    with pytest.raises(UnattendedError):
        is_never_delegable("free_money")


def test_never_delegable_policy_matches_the_frozen_kind_list() -> None:
    """Covers: RD-1 requirement 2 the policy map and the kind tuple stay in step."""
    assert tuple(NEVER_DELEGABLE_POLICY) == NEVER_DELEGABLE_KINDS


# --------------------------------------------------------------------------
# Exact-scope matching: no analogy, no prefix, no wildcards
# --------------------------------------------------------------------------


def test_exact_scope_match_applies_the_standing_decision() -> None:
    """Covers: RD-1 requirement 1 exact scope plus exact key applies the answer."""
    resolution = resolve_decision(_sheet(_standing()), _decision(), now=NOW, current_turn=1)
    assert resolution.disposition is DecisionDisposition.APPLY_STANDING_DECISION
    assert resolution.applied_standing_decision_id == "sd-1"
    assert resolution.owner_words == "Yes, wave 3 may rewrite these tests."
    assert resolution.push_allowed is False


def test_different_path_does_not_match() -> None:
    """Covers: RD-1 requirement 1 decisions are never applied by path analogy."""
    sheet = _sheet(_standing(scope=_scope(paths=("src/a.py",))))
    decision = _decision(scope=_scope(paths=("src/a/b.py",)))
    resolution = resolve_decision(sheet, decision, now=NOW, current_turn=1)
    assert resolution.disposition is DecisionDisposition.ESCALATE_OWNER
    assert "no standing decision matches" in resolution.explanation[1]


def test_parent_path_prefix_does_not_match() -> None:
    """Covers: RD-1 requirement 1 a parent path is not the same scope."""
    sheet = _sheet(_standing(scope=_scope(paths=("src",))))
    decision = _decision(scope=_scope(paths=("src", "tests")))
    resolution = resolve_decision(sheet, decision, now=NOW, current_turn=1)
    assert resolution.disposition is DecisionDisposition.ESCALATE_OWNER


def test_different_task_or_project_does_not_match() -> None:
    """Covers: RD-1 requirement 1 task and project are part of the exact scope."""
    for other in (_scope(task="task-2"), _scope(project="proj-2")):
        resolution = resolve_decision(
            _sheet(_standing(scope=_scope())),
            _decision(scope=other),
            now=NOW,
            current_turn=1,
        )
        assert resolution.disposition is DecisionDisposition.ESCALATE_OWNER


def test_different_decision_key_does_not_match() -> None:
    """Covers: RD-1 requirement 1 the decision key must match exactly too."""
    resolution = resolve_decision(
        _sheet(_standing(decision_key="other_key")),
        _decision(decision_key=KEY),
        now=NOW,
        current_turn=1,
    )
    assert resolution.disposition is DecisionDisposition.ESCALATE_OWNER


def test_scope_paths_order_is_canonicalised_but_membership_is_exact() -> None:
    """Covers: RD-1 requirement 1 the same path set matches regardless of order."""
    left = DecisionScope(project_id=PROJECT, task_id=TASK, paths=("b.py", "a.py"))
    right = DecisionScope(project_id=PROJECT, task_id=TASK, paths=("a.py", "b.py"))
    assert left == right
    assert left.matches_exactly(right)


@pytest.mark.parametrize(
    "path",
    ["src/*.py", "src/**", "*.py", "src/[ab].py", "src/{a,b}.py", "/abs/a.py", "./a.py", "a/"],
)
def test_wildcard_and_non_canonical_scope_paths_are_refused(path: str) -> None:
    """Covers: RD-1 requirement 1 wildcards and non-canonical paths fail closed."""
    with pytest.raises(UnattendedError):
        _scope(paths=(path,))


@pytest.mark.parametrize("identifier", ["proj*", "proj?", "proj[1]", "a b", ""])
def test_wildcard_and_malformed_identifiers_are_refused(identifier: str) -> None:
    """Covers: RD-1 requirement 1 identifiers cannot carry wildcards."""
    with pytest.raises(UnattendedError):
        _scope(project=identifier)


def test_matches_exactly_requires_a_scope() -> None:
    """Covers: RD-1 requirement 1 matching never guesses across types."""
    with pytest.raises(UnattendedError):
        _scope().matches_exactly("src/a.py")


def test_scope_as_dict_round_trips_the_exact_fields() -> None:
    """Covers: RD-1 requirement 1 the scope projection is exact and ordered."""
    scope = _scope(paths=("tests/test_a.py", "src/a.py"))
    assert scope.as_dict() == {
        "project_id": PROJECT,
        "task_id": TASK,
        "paths": ["src/a.py", "tests/test_a.py"],
    }


# --------------------------------------------------------------------------
# Standing decisions: expiration, lifecycle, owner words
# --------------------------------------------------------------------------


def test_standing_decision_expires_at_its_absolute_instant() -> None:
    """Covers: RD-1 requirement 1 expires_at is live strictly before the instant."""
    standing = _standing(expires_at="2026-10-18T08:00:00+00:00")
    assert standing.is_live(now="2026-10-18T07:59:59+00:00", current_turn=1) is True
    assert standing.is_expired(now="2026-10-18T08:00:00+00:00", current_turn=1) is True


def test_expired_standing_decision_falls_through_to_escalation() -> None:
    """Covers: RD-1 requirement 1 an expired answer is never applied."""
    sheet = _sheet(_standing(expires_at="2026-10-18T05:00:00+00:00"))
    resolution = resolve_decision(sheet, _decision(), now=NOW, current_turn=1)
    assert resolution.disposition is DecisionDisposition.ESCALATE_OWNER
    assert "has expired" in resolution.explanation[1]


def test_standing_decision_ttl_turns_window() -> None:
    """Covers: RD-1 requirement 1 ttl_turns is a bounded, exact turn window."""
    standing = _standing(expires_at=None, granted_at_turn=10, ttl_turns=5)
    assert standing.is_live(now=NOW, current_turn=10) is True
    assert standing.is_live(now=NOW, current_turn=14) is True
    assert standing.is_expired(now=NOW, current_turn=15) is True


def test_unbounded_standing_decision_is_refused() -> None:
    """Covers: RD-1 requirement 1 a standing permission must always expire."""
    with pytest.raises(DecisionSheetError):
        _standing(expires_at=None, ttl_turns=None)


def test_ttl_turns_without_granted_turn_is_refused() -> None:
    """Covers: RD-1 requirement 1 turn expiry needs its exact start turn."""
    with pytest.raises(DecisionSheetError):
        _standing(expires_at=None, ttl_turns=3)


def test_zero_ttl_turns_is_refused() -> None:
    """Covers: RD-1 requirement 1 a zero-turn permission is meaningless."""
    with pytest.raises(UnattendedError):
        _standing(expires_at=None, granted_at_turn=1, ttl_turns=0)


@pytest.mark.parametrize("instant", ["2026-10-18T06:00:00", "not-a-time", "2026-10-18"])
def test_naive_or_malformed_instants_are_refused(instant: str) -> None:
    """Covers: RD-1 requirement 1 naively timed bounds are ambiguous and refused."""
    with pytest.raises(UnattendedError):
        _standing(expires_at=instant)


def test_only_the_owner_may_author_a_standing_decision() -> None:
    """Covers: RD-1 requirement 1 sheet answers are owner answers, never a model's."""
    with pytest.raises(DecisionSheetError):
        _standing(answered_by="orchestrator")


def test_owner_words_are_recorded_verbatim() -> None:
    """Covers: RD-1 requirement 1 owner exact words are stored, never paraphrased."""
    words = "Yes: wave 3 may rewrite tests under ADR-0002 D5.\nSkip the wave on budget exhaustion."
    standing = _standing(owner_words=words)
    resolution = resolve_decision(_sheet(standing), _decision(), now=NOW, current_turn=1)
    assert resolution.owner_words == words


@pytest.mark.parametrize("words", ["", "   "])
def test_empty_owner_words_are_refused(words: str) -> None:
    """Covers: RD-1 requirement 1 a standing decision must carry the owner's words."""
    with pytest.raises(UnattendedError):
        _standing(owner_words=words)


def test_standing_decision_as_dict_is_exact() -> None:
    """Covers: RD-1 requirement 1 the standing decision projection is lossless."""
    standing = _standing()
    assert standing.as_dict()["owner_words"] == standing.owner_words
    assert standing.as_dict()["answered_by"] == "owner"


# --------------------------------------------------------------------------
# Decision sheet integrity
# --------------------------------------------------------------------------


def test_duplicate_scope_and_key_pair_is_refused() -> None:
    """Covers: RD-1 requirement 1 matching must be unambiguous."""
    with pytest.raises(DecisionSheetError):
        _sheet(_standing(), _standing(standing_decision_id="sd-2"))


def test_duplicate_standing_decision_id_is_refused() -> None:
    """Covers: RD-1 requirement 1 standing decision ids are unique."""
    with pytest.raises(DecisionSheetError):
        _sheet(_standing(), _standing(decision_key="other_key"))


def test_two_keys_for_one_scope_are_allowed() -> None:
    """Covers: RD-1 requirement 1 several predictable decisions share one scope."""
    sheet = _sheet(_standing(), _standing(standing_decision_id="sd-2", decision_key="skip_wave"))
    assert sheet.find(KEY, _scope()) is not None
    assert sheet.find("skip_wave", _scope()) is not None
    assert sheet.find("unknown_key", _scope()) is None


def test_run_id_is_validated() -> None:
    """Covers: RD-1 requirement 1 a sheet belongs to one identified run."""
    with pytest.raises(UnattendedError):
        _sheet(run_id="run 7")


def test_sheet_as_dict_lists_every_standing_decision() -> None:
    """Covers: RD-1 requirement 1 the frozen sheet is inspectable."""
    payload = _sheet(_standing()).as_dict()
    assert payload["run_id"] == "run-7"
    assert len(cast("list[Any]", payload["standing_decisions"])) == 1


# --------------------------------------------------------------------------
# Class 3: never-delegable parks only its branch
# --------------------------------------------------------------------------


@pytest.mark.parametrize("kind", list(NeverDelegableKind))
def test_never_delegable_parks_its_own_scope(kind: NeverDelegableKind) -> None:
    """Covers: RD-1 requirement 2/3 every never-delegable kind parks its branch."""
    resolution = resolve_decision(
        _sheet(), _decision(never_delegable=kind), now=NOW, current_turn=1
    )
    assert resolution.disposition is DecisionDisposition.PARK_BRANCH
    assert resolution.autonomy_class is DecisionClass.CLASS_3_NEVER_DELEGABLE
    assert resolution.parks_scope == _scope()
    assert resolution.push_allowed is False


def test_never_delegable_beats_a_matching_standing_decision() -> None:
    """Covers: RD-1 requirement 2 the sheet can never widen the never-delegable list."""
    resolution = resolve_decision(
        _sheet(_standing()),
        _decision(never_delegable=NeverDelegableKind.TEST_GATE),
        now=NOW,
        current_turn=1,
    )
    assert resolution.disposition is DecisionDisposition.PARK_BRANCH
    assert resolution.applied_standing_decision_id is None


def test_never_delegable_beats_green_acceptance_facts() -> None:
    """Covers: RD-1 requirement 3 a never-delegable decision is never auto-accepted."""
    resolution = resolve_decision(
        _sheet(),
        _decision(
            never_delegable=NeverDelegableKind.PUBLISHING,
            acceptance=_acceptance(),
            recommended_option="publish it",
            alternatives=("wait",),
        ),
        now=NOW,
        current_turn=1,
    )
    assert resolution.disposition is DecisionDisposition.PARK_BRANCH


def test_never_delegable_parks_in_supervised_mode_too() -> None:
    """Covers: RD-1 requirement 3 a non-autonomous mode cannot bypass the list."""
    resolution = resolve_decision(
        _sheet(),
        _decision(never_delegable=NeverDelegableKind.CREDENTIALS),
        now=NOW,
        current_turn=1,
        authority_mode=AuthorityMode.SUPERVISED,
    )
    assert resolution.disposition is DecisionDisposition.PARK_BRANCH


# --------------------------------------------------------------------------
# Class 1: auto-acceptance only on gate + cold review + green checklist
# --------------------------------------------------------------------------


def test_class_1_accepts_only_a_fully_green_candidate() -> None:
    """Covers: RD-1 requirement 3 Class 1 gate + cold review + checklist green."""
    resolution = resolve_decision(
        _sheet(), _decision(acceptance=_acceptance()), now=NOW, current_turn=1
    )
    assert resolution.disposition is DecisionDisposition.AUTO_ACCEPT_LOCAL_COMMIT
    assert resolution.autonomy_class is DecisionClass.CLASS_1_GATED_ACCEPT
    assert resolution.audit_required is True
    assert resolution.push_allowed is False
    assert "local commit" in resolution.explanation[-2]


@pytest.mark.parametrize(
    "field_name",
    ["gate_passed", "cold_review_passed", "checklist_green", "scope_authorized"],
)
def test_class_1_requires_every_green_condition(field_name: str) -> None:
    """Covers: RD-1 requirement 3 any red condition forbids the auto-acceptance."""
    decision = _decision(acceptance=_acceptance(**{field_name: False}))
    resolution = resolve_decision(_sheet(), decision, now=NOW, current_turn=1)
    assert resolution.disposition is DecisionDisposition.ESCALATE_OWNER
    assert field_name in " ".join(resolution.explanation)


def test_class_1_requires_evidence() -> None:
    """Covers: RD-1 requirement 3 an acceptance without evidence is not taken alone."""
    decision = _decision(acceptance=_acceptance(), evidence_paths=())
    resolution = resolve_decision(_sheet(), decision, now=NOW, current_turn=1)
    assert resolution.disposition is DecisionDisposition.ESCALATE_OWNER


def test_class_1_is_not_taken_in_supervised_mode() -> None:
    """Covers: RD-1 requirement 3 every other authority mode asks the owner."""
    resolution = resolve_decision(
        _sheet(),
        _decision(acceptance=_acceptance()),
        now=NOW,
        current_turn=1,
        authority_mode=AuthorityMode.SUPERVISED,
    )
    assert resolution.disposition is DecisionDisposition.ESCALATE_OWNER


def test_class_1_resolution_never_allows_a_push() -> None:
    """Covers: RD-1 requirement 3 Class 1 executes as a local commit only, never push."""
    resolution = resolve_decision(
        _sheet(), _decision(acceptance=_acceptance()), now=NOW, current_turn=1
    )
    assert resolution.push_allowed is False
    with pytest.raises(UnattendedError):
        dataclasses.replace(resolution, push_allowed=True)


# --------------------------------------------------------------------------
# Class 2: the recommended option, with evidence and rejected alternatives
# --------------------------------------------------------------------------


def test_class_2_takes_the_recommended_option() -> None:
    """Covers: RD-1 requirement 3 Class 2 recommendation is taken and audited."""
    decision = _decision(
        recommended_option="skip wave 4 and its dependents",
        recommended_rationale="budget is nearly exhausted",
        alternatives=("extend the budget", "wait for the reset"),
    )
    resolution = resolve_decision(_sheet(), decision, now=NOW, current_turn=1)
    assert resolution.disposition is DecisionDisposition.TAKE_RECOMMENDED_OPTION
    assert resolution.autonomy_class is DecisionClass.CLASS_2_RECOMMENDED
    assert resolution.chosen_option == "skip wave 4 and its dependents"
    assert resolution.rejected_alternatives == ("extend the budget", "wait for the reset")
    assert resolution.audit_required is True


def test_class_2_requires_a_rejected_alternative() -> None:
    """Covers: RD-1 requirement 3 an unaudited recommendation is escalated."""
    decision = _decision(recommended_option="skip wave 4", alternatives=())
    resolution = resolve_decision(_sheet(), decision, now=NOW, current_turn=1)
    assert resolution.disposition is DecisionDisposition.ESCALATE_OWNER


def test_class_2_requires_evidence() -> None:
    """Covers: RD-1 requirement 3 a recommendation without evidence is escalated."""
    decision = _decision(
        recommended_option="skip wave 4",
        alternatives=("extend the budget",),
        evidence_paths=(),
    )
    resolution = resolve_decision(_sheet(), decision, now=NOW, current_turn=1)
    assert resolution.disposition is DecisionDisposition.ESCALATE_OWNER


def test_class_2_is_not_taken_in_supervised_mode() -> None:
    """Covers: RD-1 requirement 3 supervised mode asks the owner for unhandled decisions."""
    resolution = resolve_decision(
        _sheet(),
        _decision(recommended_option="skip wave 4", alternatives=("extend",)),
        now=NOW,
        current_turn=1,
        authority_mode=AuthorityMode.SUPERVISED,
    )
    assert resolution.disposition is DecisionDisposition.ESCALATE_OWNER


def test_alternatives_without_a_recommendation_are_refused() -> None:
    """Covers: RD-1 requirement 3 rejected alternatives belong to a recommendation."""
    with pytest.raises(UnattendedError):
        _decision(alternatives=("extend",))


def test_rationale_without_a_recommendation_is_refused() -> None:
    """Covers: RD-1 requirement 3 a rationale belongs to a recommendation."""
    with pytest.raises(UnattendedError):
        _decision(recommended_rationale="because")


# --------------------------------------------------------------------------
# Class 3 escalation and determinism
# --------------------------------------------------------------------------


def test_unhandled_decision_escalates_to_the_owner() -> None:
    """Covers: RD-1 requirement 3 every other unhandled decision reaches the owner."""
    resolution = resolve_decision(_sheet(), _decision(), now=NOW, current_turn=1)
    assert resolution.disposition is DecisionDisposition.ESCALATE_OWNER
    assert resolution.autonomy_class is DecisionClass.CLASS_3_NEVER_DELEGABLE
    assert resolution.parks_scope is None
    assert resolution.audit_required is False


def test_resolution_requires_the_right_input_types() -> None:
    """Covers: RD-1 requirement 3 resolution fails closed on malformed input."""
    with pytest.raises(DecisionSheetError):
        resolve_decision(cast("Any", "sheet"), _decision(), now=NOW, current_turn=1)
    with pytest.raises(UnattendedError):
        resolve_decision(_sheet(), cast("Any", "decision"), now=NOW, current_turn=1)


def test_resolution_is_deterministic_and_versioned() -> None:
    """Covers: RD-1 requirement 3 identical input yields a byte-identical decision."""
    first = resolve_decision(_sheet(), _decision(), now=NOW, current_turn=1)
    second = resolve_decision(_sheet(), _decision(), now=NOW, current_turn=1)
    assert first == second
    assert first.canonical_json() == second.canonical_json()
    assert first.policy_version == UNATTENDED_PLANS_VERSION


def test_resolution_does_not_mutate_its_inputs() -> None:
    """Covers: RD-1 requirement 3 the policy is a pure projection."""
    sheet = _sheet(_standing())
    decision = _decision()
    before_sheet = _sheet(_standing())
    before_decision = _decision()
    resolve_decision(sheet, decision, now=NOW, current_turn=1)
    assert sheet == before_sheet
    assert decision == before_decision


def test_configuration_dataclasses_are_frozen() -> None:
    """Covers: RD-1 requirement 3 a journalled record cannot be mutated afterwards."""
    scope = _scope()
    field_name = "project_id"
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(scope, field_name, "other")


# --------------------------------------------------------------------------
# The audit list
# --------------------------------------------------------------------------


def test_audit_entries_collect_only_class_1_and_class_2() -> None:
    """Covers: RD-1 requirement 3 the Orchestrator-alone audit list is precise."""
    resolutions = (
        resolve_decision(_sheet(), _decision(acceptance=_acceptance()), now=NOW, current_turn=1),
        resolve_decision(
            _sheet(),
            _decision(
                decision_id="d-2",
                recommended_option="skip wave 4",
                alternatives=("extend",),
            ),
            now=NOW,
            current_turn=1,
        ),
        resolve_decision(_sheet(), _decision(decision_id="d-3"), now=NOW, current_turn=1),
    )
    entries = build_audit_entries(resolutions)
    assert [entry.autonomy_class for entry in entries] == [
        DecisionClass.CLASS_1_GATED_ACCEPT,
        DecisionClass.CLASS_2_RECOMMENDED,
    ]
    assert entries[1].rejected_alternatives == ("extend",)
    assert entries[1].evidence_paths == ("evidence/g1.txt",)


def test_audit_entry_shape_is_exact() -> None:
    """Covers: RD-1 requirement 3 an audit entry names the class, option and evidence."""
    resolution = resolve_decision(
        _sheet(),
        _decision(recommended_option="skip", alternatives=("extend",)),
        now=NOW,
        current_turn=1,
    )
    (entry,) = build_audit_entries((resolution,))
    assert isinstance(entry, AutonomousAuditEntry)
    assert entry.as_dict()["chosen_option"] == "skip"


def test_audit_builder_rejects_malformed_input() -> None:
    """Covers: RD-1 requirement 3 the audit list fails closed, not silently."""
    with pytest.raises(UnattendedError):
        build_audit_entries(cast("Any", "not-a-sequence"))
    with pytest.raises(UnattendedError):
        build_audit_entries([cast("Any", "not-a-resolution")])


# --------------------------------------------------------------------------
# Circuit breaker: three consecutive failures halt the plan
# --------------------------------------------------------------------------


def test_circuit_breaker_limit_is_three() -> None:
    """Covers: RD-1 requirement 4 halt after 3 consecutive task failures."""
    assert CONSECUTIVE_FAILURE_LIMIT == 3


def test_two_failures_do_not_halt() -> None:
    """Covers: RD-1 requirement 4 two failures is still a runnable plan."""
    progress = RunProgress().after_task(passed=False).after_task(passed=False)
    assert progress.consecutive_failures == 2
    assert progress.circuit_open is False
    assert progress.halted is False


def test_three_consecutive_failures_halt() -> None:
    """Covers: RD-1 requirement 4 the third consecutive failure halts the plan."""
    progress = RunProgress()
    for _ in range(CONSECUTIVE_FAILURE_LIMIT):
        progress = progress.after_task(passed=False)
    assert progress.consecutive_failures == 3
    assert progress.circuit_open is True
    assert progress.halted is True


def test_a_pass_resets_the_consecutive_counter() -> None:
    """Covers: RD-1 requirement 4 failures must be consecutive to trip the breaker."""
    progress = (
        RunProgress()
        .after_task(passed=False)
        .after_task(passed=False)
        .after_task(passed=True)
        .after_task(passed=False)
    )
    assert progress.consecutive_failures == 1
    assert progress.halted is False


def test_halting_is_terminal() -> None:
    """Covers: RD-1 requirement 4 a halt is never silently resumed by a later pass."""
    halted = RunProgress(consecutive_failures=CONSECUTIVE_FAILURE_LIMIT, halted=True)
    assert halted.after_task(passed=True).halted is True
    assert halted.after_task(passed=False).halted is True


def test_progress_as_dict_reports_the_breaker() -> None:
    """Covers: RD-1 requirement 4 the breaker state is visible to the report."""
    payload = RunProgress(consecutive_failures=3, halted=True).as_dict()
    assert payload == {"consecutive_failures": 3, "circuit_open": True, "halted": True}


# --------------------------------------------------------------------------
# No-silent-stall alarms
# --------------------------------------------------------------------------


def _observation(**overrides: Any) -> StallObservation:
    fields: dict[str, Any] = {
        "run_id": "run-7",
        "last_heartbeat_at": NOW,
        "now": "2026-10-18T06:05:00+00:00",
        "stall_threshold_seconds": 600,
        "channels": (NotificationChannel.LOG, NotificationChannel.TELEGRAM),
    }
    fields.update(overrides)
    return StallObservation(**fields)


def test_healthy_run_raises_no_alarm() -> None:
    """Covers: RD-1 requirement 4 a live, reporting run is silent."""
    assert detect_stall(_observation()) is None


def test_idle_at_the_threshold_raises_an_alarm() -> None:
    """Covers: RD-1 requirement 4 inactivity raises a loud alarm."""
    alarm = detect_stall(_observation(now="2026-10-18T06:10:00+00:00"))
    assert isinstance(alarm, StallAlarm)
    assert alarm.severity is AlarmSeverity.ALARM
    assert alarm.idle_seconds == 600
    assert alarm.channels == (NotificationChannel.LOG, NotificationChannel.TELEGRAM)


def test_extreme_idle_raises_a_critical_alarm() -> None:
    """Covers: RD-1 requirement 4 the alarm escalates when the stall deepens."""
    alarm = detect_stall(
        _observation(now="2026-10-18T06:20:00+00:00", stall_threshold_seconds=600)
    )
    assert alarm is not None
    assert alarm.severity is AlarmSeverity.CRITICAL
    assert alarm.idle_seconds == STALL_CRITICAL_MULTIPLIER * 600


def test_a_dead_supervisor_is_critical_even_when_fresh() -> None:
    """Covers: RD-1 requirement 4 a run is never left without a live supervisor."""
    alarm = detect_stall(_observation(supervisor_alive=False))
    assert alarm is not None
    assert alarm.severity is AlarmSeverity.CRITICAL
    assert "no live supervisor" in alarm.reason


def test_stalled_scopes_raise_an_alarm_even_when_fresh() -> None:
    """Covers: RD-1 requirement 4 a blocked state is not a silent state."""
    alarm = detect_stall(_observation(stalled_scopes=(_scope(),)))
    assert alarm is not None
    assert alarm.stalled_scopes == (_scope(),)
    assert "stalled scope" in alarm.reason


def test_a_run_without_an_alarm_channel_is_refused() -> None:
    """Covers: RD-1 requirement 4 a run that can raise no alarm is a silent stall."""
    with pytest.raises(StallSupervisionError):
        _observation(channels=())


def test_duplicate_alarm_channels_are_refused() -> None:
    """Covers: RD-1 requirement 4 alarm channels are a set, not a repeat list."""
    with pytest.raises(UnattendedError):
        _observation(channels=(NotificationChannel.LOG, NotificationChannel.LOG))


def test_a_future_heartbeat_is_refused() -> None:
    """Covers: RD-1 requirement 4 impossible supervision telemetry fails closed."""
    with pytest.raises(StallSupervisionError):
        _observation(last_heartbeat_at="2026-10-18T07:00:00+00:00")


@pytest.mark.parametrize("threshold", [0, -1])
def test_a_non_positive_threshold_is_refused(threshold: int) -> None:
    """Covers: RD-1 requirement 4 every step has a real timeout."""
    with pytest.raises(UnattendedError):
        _observation(stall_threshold_seconds=threshold)


def test_alarm_message_is_single_line_and_bounded() -> None:
    """Covers: RD-1 requirement 4 the alarm is loud, safe and channel-portable."""
    alarm = detect_stall(_observation(now="2026-10-18T09:00:00+00:00", stalled_scopes=(_scope(),)))
    assert alarm is not None
    assert "\n" not in alarm.message and "\r" not in alarm.message
    assert len(alarm.message) <= MAX_ALARM_MESSAGE_LENGTH
    assert alarm.message.startswith("STALL run=run-7")
    assert alarm.as_dict()["severity"] == alarm.severity.value


def test_detect_stall_requires_an_observation() -> None:
    """Covers: RD-1 requirement 4 stall detection is typed, not duck-typed."""
    with pytest.raises(StallSupervisionError):
        detect_stall(cast("Any", {"run_id": "run-7"}))


# --------------------------------------------------------------------------
# Preflight: budget, wall clock, scopes, worker caps, alarm channel
# --------------------------------------------------------------------------


def test_a_fully_configured_run_is_ready() -> None:
    """Covers: RD-1 requirement 4 a complete readiness check passes."""
    result = run_preflight(_preflight())
    assert isinstance(result, PreflightResult)
    assert result.ready is True
    assert result.blockers == ()
    assert result.run_id == "run-7"


def test_unset_budget_refuses_the_start() -> None:
    """Covers: RD-1 requirement 4 the owner must set the budget before a start."""
    result = run_preflight(_preflight(budget_limit_units=0))
    assert result.ready is False
    assert PreflightBlockerCode.BUDGET_NOT_SET in {item.code for item in result.blockers}


def test_exhausted_budget_refuses_the_start() -> None:
    """Covers: RD-1 requirement 4 an exhausted budget prevents the start."""
    result = run_preflight(_preflight(budget_limit_units=100, budget_spent_units=101))
    assert PreflightBlockerCode.BUDGET_EXCEEDED in {item.code for item in result.blockers}


def test_unset_wall_clock_refuses_the_start() -> None:
    """Covers: RD-1 requirement 4 the owner must bound the wall clock."""
    result = run_preflight(_preflight(wall_clock_limit_seconds=0))
    assert PreflightBlockerCode.WALL_CLOCK_NOT_SET in {item.code for item in result.blockers}


def test_reached_wall_clock_refuses_the_start() -> None:
    """Covers: RD-1 requirement 4 reaching the wall-clock limit prevents the start."""
    result = run_preflight(
        _preflight(wall_clock_limit_seconds=3600, wall_clock_elapsed_seconds=3600)
    )
    assert PreflightBlockerCode.WALL_CLOCK_EXCEEDED in {item.code for item in result.blockers}


def test_no_authorized_scope_refuses_the_start() -> None:
    """Covers: RD-1 requirement 4 the owner must authorize explicit file scopes."""
    result = run_preflight(_preflight(authorized_paths=()))
    assert PreflightBlockerCode.NO_AUTHORIZED_SCOPE in {item.code for item in result.blockers}


def test_open_or_wildcard_scopes_cannot_even_be_constructed() -> None:
    """Covers: RD-1 requirement 4 an open scope is refused before the preflight."""
    with pytest.raises(UnattendedError):
        _preflight(authorized_paths=("src/**",))
    with pytest.raises(UnattendedError):
        _preflight(authorized_paths=("/",))


def test_no_alarm_channel_refuses_the_start() -> None:
    """Covers: RD-1 requirement 4 no silent stall may ever start."""
    result = run_preflight(_preflight(notification_channels=()))
    assert PreflightBlockerCode.NO_STALL_CHANNEL in {item.code for item in result.blockers}


def test_unset_worker_cap_refuses_the_start() -> None:
    """Covers: RD-1 requirement 4 the owner must set the worker cap."""
    result = run_preflight(_preflight(worker_cap=0, active_workers=0))
    assert PreflightBlockerCode.WORKER_CAP_NOT_SET in {item.code for item in result.blockers}


def test_worker_cap_overflow_refuses_the_start() -> None:
    """Covers: RD-1 requirement 4 more active workers than the cap prevents the start."""
    result = run_preflight(_preflight(worker_cap=2, active_workers=3))
    assert PreflightBlockerCode.WORKER_CAP_EXCEEDED in {item.code for item in result.blockers}


def test_a_fully_occupied_worker_cap_only_warns() -> None:
    """Covers: RD-1 requirement 4 a full cap warns; it does not block."""
    result = run_preflight(_preflight(worker_cap=2, active_workers=2))
    assert result.ready is True
    assert any("fully occupied" in warning for warning in result.warnings)


def test_a_sheet_for_another_run_refuses_the_start() -> None:
    """Covers: RD-1 requirement 4 the frozen sheet must belong to the starting run."""
    result = run_preflight(_preflight(decision_sheet=_sheet(run_id="run-9")))
    assert PreflightBlockerCode.RUN_ID_MISMATCH in {item.code for item in result.blockers}


def test_nearly_spent_resources_warn_without_blocking() -> None:
    """Covers: RD-1 requirement 4 warnings never block a start."""
    result = run_preflight(
        _preflight(
            budget_limit_units=100,
            budget_spent_units=PREFLIGHT_WARN_PERCENT,
            wall_clock_limit_seconds=3600,
            wall_clock_elapsed_seconds=3000,
        )
    )
    assert result.ready is True
    assert len(result.warnings) == 2


def test_reaching_the_budget_limit_is_a_blocker_not_a_warning() -> None:
    """Covers: RD-1 requirement 4 the boundary between warning and blocker is exact."""
    at_limit = run_preflight(_preflight(budget_limit_units=100, budget_spent_units=100))
    assert at_limit.ready is True
    over = run_preflight(_preflight(budget_limit_units=100, budget_spent_units=101))
    assert over.ready is False


def test_preflight_ready_is_equivalent_to_having_no_blockers() -> None:
    """Covers: RD-1 requirement 4 readiness is exactly the absence of blockers."""
    for request in (
        _preflight(),
        _preflight(budget_limit_units=0),
        _preflight(worker_cap=0, active_workers=0),
    ):
        result = run_preflight(request)
        assert result.ready == (len(result.blockers) == 0)


def test_preflight_rejects_malformed_inputs() -> None:
    """Covers: RD-1 requirement 4 preflight facts are validated, never assumed."""
    with pytest.raises(UnattendedError):
        _preflight(budget_spent_units=-1)
    with pytest.raises(UnattendedError):
        _preflight(active_workers=-1)
    with pytest.raises(UnattendedError):
        _preflight(notification_channels=(NotificationChannel.LOG, NotificationChannel.LOG))
    with pytest.raises(UnattendedError):
        run_preflight(cast("Any", "not-a-request"))


def test_preflight_as_dict_is_exact() -> None:
    """Covers: RD-1 requirement 4 the readiness report is machine-readable."""
    result = run_preflight(_preflight(worker_cap=0, active_workers=0))
    payload = result.as_dict()
    assert payload["ready"] is False
    assert payload["run_id"] == "run-7"
    assert payload["policy_version"] == UNATTENDED_PLANS_VERSION


# --------------------------------------------------------------------------
# Plan impact: a park holds only its branch and its dependents
# --------------------------------------------------------------------------


def _tasks() -> tuple[PlanTaskState, ...]:
    base = _scope(task="task-a")
    middle = _scope(task="task-b")
    independent = _scope(task="task-c")
    return (
        PlanTaskState(scope=base),
        PlanTaskState(scope=middle, depends_on=(base,)),
        PlanTaskState(scope=independent),
    )


def test_a_park_holds_only_its_branch_and_dependents() -> None:
    """Covers: RD-1 requirement 2 a never-delegable park never stalls the plan."""
    impact = compute_plan_impact(_tasks(), parked_scopes=(_scope(task="task-a"),))
    assert isinstance(impact, PlanImpact)
    assert impact.parked == (_scope(task="task-a"),)
    assert impact.held == (_scope(task="task-a"), _scope(task="task-b"))
    assert impact.runnable == (_scope(task="task-c"),)
    assert impact.plan_stalled is False


def test_an_escalation_holds_its_dependents_too() -> None:
    """Covers: RD-1 requirement 3 an owner question stops only dependent work."""
    impact = compute_plan_impact(_tasks(), escalated_scopes=(_scope(task="task-a"),))
    assert impact.escalated == (_scope(task="task-a"),)
    assert impact.held == (_scope(task="task-a"), _scope(task="task-b"))
    assert impact.runnable == (_scope(task="task-c"),)


def test_a_plan_is_stalled_only_when_nothing_remains_runnable() -> None:
    """Covers: RD-1 requirement 4 a fully held plan is reported as stalled."""
    impact = compute_plan_impact(
        _tasks(),
        parked_scopes=(_scope(task="task-a"), _scope(task="task-c")),
    )
    assert impact.runnable == ()
    assert impact.plan_stalled is True


def test_an_empty_plan_is_not_reported_as_stalled() -> None:
    """Covers: RD-1 requirement 4 no work is not the same as a stall."""
    impact = compute_plan_impact(())
    assert impact.plan_stalled is False
    assert impact.runnable == ()


def test_an_unknown_parked_scope_is_refused() -> None:
    """Covers: RD-1 requirement 2 the runner never holds a scope it cannot see."""
    with pytest.raises(UnattendedError):
        compute_plan_impact(_tasks(), parked_scopes=(_scope(task="task-z"),))


def test_an_unknown_dependency_is_refused() -> None:
    """Covers: RD-1 requirement 2 an unresolvable dependency fails closed."""
    with pytest.raises(UnattendedError):
        compute_plan_impact(
            (PlanTaskState(scope=_scope(task="task-a"), depends_on=(_scope(task="task-z"),)),)
        )


def test_a_dependency_cycle_is_refused() -> None:
    """Covers: RD-1 requirement 2 a cyclic plan fails closed instead of looping."""
    first = _scope(task="task-a")
    second = _scope(task="task-b")
    with pytest.raises(UnattendedError):
        compute_plan_impact(
            (
                PlanTaskState(scope=first, depends_on=(second,)),
                PlanTaskState(scope=second, depends_on=(first,)),
            )
        )


def test_a_task_cannot_depend_on_itself() -> None:
    """Covers: RD-1 requirement 2 a self-cycle fails closed at construction."""
    with pytest.raises(UnattendedError):
        PlanTaskState(scope=_scope(task="task-a"), depends_on=(_scope(task="task-a"),))


def test_duplicate_task_scopes_are_refused() -> None:
    """Covers: RD-1 requirement 2 the plan graph has one node per task."""
    with pytest.raises(UnattendedError):
        compute_plan_impact((PlanTaskState(scope=_scope()), PlanTaskState(scope=_scope())))


def test_mixed_project_graphs_are_refused() -> None:
    """Covers: RD-1 requirement 2 one impact model serves one project."""
    with pytest.raises(UnattendedError):
        compute_plan_impact(
            (PlanTaskState(scope=_scope()), PlanTaskState(scope=_scope(project="proj-2")))
        )


def test_impact_projection_is_exact() -> None:
    """Covers: RD-1 requirement 2 the impact projection is inspectable."""
    payload = compute_plan_impact(
        _tasks(), parked_scopes=(_scope(task="task-a"),)
    ).as_dict()
    assert payload["plan_stalled"] is False
    assert len(cast("list[Any]", payload["held"])) == 2


# --------------------------------------------------------------------------
# Morning report
# --------------------------------------------------------------------------


def _report_inputs() -> tuple[list[Decision], list[DecisionResolution]]:
    decisions = [
        Decision(
            decision_id="d-1",
            scope=_scope(task="task-1"),
            decision_key=KEY,
            question="Rewrite the tests?",
        ),
        _decision(decision_id="d-2", scope=_scope(task="task-2"), acceptance=_acceptance()),
        _decision(
            decision_id="d-3",
            scope=_scope(task="task-3"),
            never_delegable=NeverDelegableKind.BUDGET_CAP,
        ),
        _decision(decision_id="d-4", scope=_scope(task="task-4")),
        _decision(
            decision_id="d-5",
            scope=_scope(task="task-5"),
            recommended_option="skip",
            alternatives=("extend",),
        ),
    ]
    sheet = _sheet(
        StandingDecision(
            standing_decision_id="sd-1",
            scope=_scope(),
            decision_key=KEY,
            owner_words="Yes, rewrite them.",
            answered_by="owner",
            expires_at=LATER,
        )
    )
    resolutions = [
        resolve_decision(sheet, decisions[0], now=NOW, current_turn=1),
        resolve_decision(sheet, decisions[1], now=NOW, current_turn=1),
        resolve_decision(sheet, decisions[2], now=NOW, current_turn=1),
        resolve_decision(sheet, decisions[3], now=NOW, current_turn=1),
        resolve_decision(sheet, decisions[4], now=NOW, current_turn=1),
    ]
    return decisions, resolutions


def test_morning_report_summarises_every_category() -> None:
    """Covers: RD-1 requirement 4 the morning report lists every action category."""
    decisions, resolutions = _report_inputs()
    alarm = detect_stall(_observation(supervisor_alive=False))
    assert alarm is not None
    report = build_morning_report(
        run_id="run-7",
        resolutions=resolutions,
        decisions=decisions,
        progress=RunProgress(consecutive_failures=3, halted=True),
        alarms=(alarm,),
    )
    assert isinstance(report, MorningReport)
    assert [item.standing_decision_id for item in report.standing_decision_actions] == ["sd-1"]
    assert report.standing_decision_actions[0].owner_words == "Yes, rewrite them."
    (accepted,) = report.accepted_candidates
    assert isinstance(accepted, AcceptedCandidateRecord)
    assert accepted.local_commit_only is True
    assert accepted.pushed is False
    assert accepted.head_sha == HEAD_A
    (parked,) = report.parked_branches
    assert parked.never_delegable is NeverDelegableKind.BUDGET_CAP
    assert len(report.escalations) == 1
    assert len(report.audit_entries) == 2
    assert report.consecutive_failures == 3
    assert report.circuit_open is True
    assert report.halted is True
    assert len(report.stall_alarms) == 1


def test_morning_report_is_byte_stable() -> None:
    """Covers: RD-1 requirement 4 identical input yields an identical report."""
    decisions, resolutions = _report_inputs()
    first = build_morning_report(run_id="run-7", resolutions=resolutions, decisions=decisions)
    second = build_morning_report(run_id="run-7", resolutions=resolutions, decisions=decisions)
    assert first.canonical_json() == second.canonical_json()
    assert first.as_dict()["policy_version"] == UNATTENDED_PLANS_VERSION


def test_morning_report_defaults_to_an_empty_run() -> None:
    """Covers: RD-1 requirement 4 a quiet run produces an empty, honest report."""
    report = build_morning_report(run_id="run-7", resolutions=())
    assert report.standing_decision_actions == ()
    assert report.accepted_candidates == ()
    assert report.parked_branches == ()
    assert report.escalations == ()
    assert report.audit_entries == ()
    assert report.stall_alarms == ()
    assert report.circuit_open is False


def test_accepted_candidate_needs_its_decision_facts() -> None:
    """Covers: RD-1 requirement 4 the report never invents acceptance facts."""
    resolution = resolve_decision(
        _sheet(), _decision(acceptance=_acceptance()), now=NOW, current_turn=1
    )
    with pytest.raises(UnattendedError):
        build_morning_report(run_id="run-7", resolutions=(resolution,))


def test_parked_branch_needs_its_decision_facts() -> None:
    """Covers: RD-1 requirement 4 the report never invents a never-delegable kind."""
    resolution = resolve_decision(
        _sheet(),
        _decision(never_delegable=NeverDelegableKind.CREDENTIALS),
        now=NOW,
        current_turn=1,
    )
    with pytest.raises(UnattendedError):
        build_morning_report(run_id="run-7", resolutions=(resolution,))


def test_report_rejects_malformed_sequences() -> None:
    """Covers: RD-1 requirement 4 the report fails closed on malformed input."""
    with pytest.raises(UnattendedError):
        build_morning_report(run_id="run-7", resolutions=cast("Any", "not-a-sequence"))
    with pytest.raises(UnattendedError):
        build_morning_report(run_id="run-7", resolutions=(), alarms=(cast("Any", "x"),))
    with pytest.raises(UnattendedError):
        build_morning_report(run_id="run-7", resolutions=(), decisions=(cast("Any", "x"),))
