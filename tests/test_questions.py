"""Owner question queue, escalation protocol and web entry point (Q-1, Step 012).

Hermetic: every store lives in ``tmp_path`` and every timestamp is injected, so
the tests never read the owner's real state, never touch the clock and never
call a provider.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import saberops.questions as questions_module
from saberops.questions import (
    ASK_EVENT_VERB,
    CFG_1_TARGET,
    ESCALATION_PARTS,
    IMPLEMENTER_ROLES,
    ORCHESTRATOR_ANSWERABLE_DECISION_CLASSES,
    QUESTION_TRANSITION_VERBS,
    QUESTIONS_ROUTER_PREFIX,
    QUESTIONS_STORE_SCHEMA_VERSION,
    STANDING_RULE_THRESHOLD,
    TASK_LIFECYCLE_CLOSE_AUTHORITIES,
    AnswerAuthority,
    AnswerAuthorityError,
    AskRole,
    DecisionClass,
    DivergenceReason,
    Escalation,
    EscalationError,
    OwnerQuestion,
    OwnerQuestionQueue,
    QuestionError,
    QuestionLedger,
    QuestionRequest,
    QuestionStateError,
    QuestionStatus,
    QuestionStoreError,
    build_questions_router,
    get_questions_store_path,
    is_orchestrator_answerable,
    question_id_for_key,
    question_view,
    request_from_payload,
    resolve_answer_authority,
)

# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------

QUEUE_PATH = "questions.json"


@pytest.fixture()
def queue(tmp_path: Path) -> OwnerQuestionQueue:
    return OwnerQuestionQueue(tmp_path / QUEUE_PATH)


def _escalation() -> Escalation:
    return Escalation(
        requirement="The lane cannot pass the gate without editing a protected test",
        proposed_expansion="Allow editing tests/test_gate_scope.py in this lane",
        smallest_compliant="Add one assertion to the existing protected test",
        consequences="An accepted ADR's test contract changes",
        recommendation="Owner decides; the Orchestrator cannot",
    )


def _request(
    key: str = "run_abc/gate-block",
    *,
    kind: str = "gate-block",
    decision_class: DecisionClass = DecisionClass.TEST_CHANGE_AGAINST_ADR,
    role: AskRole = AskRole.WORKER,
    by: str = "worker:run_abc",
    run_id: str | None = "run_abc",
    blocks: tuple[str, ...] = ("wave3/lane-a",),
    options: tuple[str, ...] = (),
    default_option: str | None = None,
    escalation: Escalation | None = None,
    supersedes: str | None = None,
) -> QuestionRequest:
    return QuestionRequest(
        key=key,
        subject="May the lane touch tests/test_gate_scope.py to fix the block?",
        kind=kind,
        decision_class=decision_class,
        raised_by_role=role,
        raised_by=by,
        escalation=_escalation() if escalation is None else escalation,
        run_id=run_id,
        blocks=blocks,
        options=options,
        default_option=default_option,
        supersedes=supersedes,
    )


def _owner_answer(
    queue: OwnerQuestionQueue,
    ref: str,
    words: str,
    *,
    at: str = "2026-10-18T10:00:00+00:00",
    chosen_option: str | None = None,
) -> OwnerQuestion:
    return queue.answer(
        ref,
        words=words,
        authority=AnswerAuthority.OWNER,
        answered_by="owner",
        answered_by_role=AskRole.OWNER,
        chosen_option=chosen_option,
        now=at,
    )


def _orchestrator_request(key: str = "review/finding-f3") -> QuestionRequest:
    return _request(
        key,
        kind="review-finding",
        decision_class=DecisionClass.REVIEW_FINDING_CLASSIFICATION,
        role=AskRole.REVIEWER,
        by="reviewer:run_abc",
        blocks=(),
        options=("accept", "reject", "defer"),
        default_option="reject",
    )


def _orchestrator_answer(
    queue: OwnerQuestionQueue,
    ref: str,
    words: str,
    *,
    by: str = "orchestrator:session-1",
    at: str = "2026-10-18T11:00:00+00:00",
) -> OwnerQuestion:
    return queue.answer(
        ref,
        words=words,
        authority=AnswerAuthority.ORCHESTRATOR,
        answered_by=by,
        answered_by_role=AskRole.ORCHESTRATOR,
        chosen_option="accept",
        evidence="review finding F3 quotes the failing assertion",
        rejected_alternative="reject the finding and fix the code instead",
        now=at,
    )


# ---------------------------------------------------------------------------
# the five-part escalation protocol
# ---------------------------------------------------------------------------


def test_escalation_requires_every_one_of_the_five_parts() -> None:
    for part in ESCALATION_PARTS:
        payload: dict[str, str] = {name: f"{name} text" for name in ESCALATION_PARTS}
        payload[part] = "   "
        with pytest.raises(EscalationError) as excinfo:
            Escalation.from_mapping(payload)
        assert part in str(excinfo.value)


def test_escalation_round_trips_through_its_mapping() -> None:
    escalation = _escalation()
    assert escalation.to_mapping() == {
        "requirement": escalation.requirement,
        "proposed_expansion": escalation.proposed_expansion,
        "smallest_compliant": escalation.smallest_compliant,
        "consequences": escalation.consequences,
        "recommendation": escalation.recommendation,
    }
    assert Escalation.from_mapping(escalation.to_mapping()) == escalation
    with pytest.raises(EscalationError):
        Escalation.from_mapping({"requirement": "only one part"})


def test_a_question_cannot_exist_without_the_five_parts() -> None:
    with pytest.raises(EscalationError):
        QuestionRequest(
            key="k",
            subject="s",
            kind="kind",
            decision_class=DecisionClass.OTHER,
            raised_by_role=AskRole.WORKER,
            raised_by="worker",
            escalation=None,  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# authority criteria
# ---------------------------------------------------------------------------


def test_answer_authority_is_derived_from_the_decision_class() -> None:
    for decision_class in ORCHESTRATOR_ANSWERABLE_DECISION_CLASSES:
        assert is_orchestrator_answerable(decision_class) is True
        assert resolve_answer_authority(decision_class) is AnswerAuthority.ORCHESTRATOR
    for decision_class in (
        DecisionClass.MOVE_MAIN,
        DecisionClass.PUSH,
        DecisionClass.QUARANTINE,
        DecisionClass.BILLING_OR_POSTURE,
        DecisionClass.TEST_CHANGE_AGAINST_ADR,
        DecisionClass.DELETE_OUTSIDE_ENGINE_FOLDERS,
        DecisionClass.APPROVE_CONFIG_PROPOSAL,
        DecisionClass.OTHER,
    ):
        assert is_orchestrator_answerable(decision_class) is False
        assert resolve_answer_authority(decision_class) is AnswerAuthority.OWNER


def test_owner_only_classes_never_resolve_to_the_orchestrator() -> None:
    assert ORCHESTRATOR_ANSWERABLE_DECISION_CLASSES.isdisjoint(
        {
            DecisionClass.MOVE_MAIN,
            DecisionClass.PUSH,
            DecisionClass.QUARANTINE,
            DecisionClass.BILLING_OR_POSTURE,
            DecisionClass.TEST_CHANGE_AGAINST_ADR,
            DecisionClass.DELETE_OUTSIDE_ENGINE_FOLDERS,
            DecisionClass.APPROVE_CONFIG_PROPOSAL,
            DecisionClass.OTHER,
        }
    )


def test_question_id_is_deterministic_and_key_derived() -> None:
    assert question_id_for_key("run_abc/gate-block") == question_id_for_key(
        "  run_abc/gate-block  "
    )
    assert question_id_for_key("a") != question_id_for_key("b")
    assert question_id_for_key("a").startswith("q-")
    with pytest.raises(QuestionError):
        question_id_for_key("   ")


# ---------------------------------------------------------------------------
# asking: keyed, idempotent, verbatim
# ---------------------------------------------------------------------------


def test_ask_is_keyed_and_idempotent(queue: OwnerQuestionQueue) -> None:
    first = queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    again = queue.ask(_request(), now="2026-10-18T09:05:00+00:00")
    assert first.question_id == again.question_id
    assert first.question_id == question_id_for_key(first.key)
    assert len(queue.questions()) == 1
    assert again.raised_at == "2026-10-18T09:00:00+00:00"
    assert again.history[0].verb == ASK_EVENT_VERB
    assert again.answerable_by is AnswerAuthority.OWNER


def test_the_same_key_with_a_different_payload_fails_closed(
    queue: OwnerQuestionQueue,
) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    conflicting = _request(kind="a-different-kind")
    with pytest.raises(QuestionError):
        queue.ask(conflicting, now="2026-10-18T09:01:00+00:00")
    assert len(queue.questions()) == 1


def test_an_answered_question_is_not_re_asked(queue: OwnerQuestionQueue) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    _owner_answer(queue, "run_abc/gate-block", "yes, allow it")
    again = queue.ask(_request(), now="2026-10-18T12:00:00+00:00")
    assert again.status is QuestionStatus.ANSWERED
    assert again.answer is not None
    assert again.answer.words == "yes, allow it"


def test_a_superseding_question_must_name_a_known_question(
    queue: OwnerQuestionQueue,
) -> None:
    with pytest.raises(QuestionError):
        queue.ask(_request("run_abc/second-block", supersedes="run_abc/nope"))
    assert queue.questions() == ()
    original = queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    superseding = queue.ask(
        _request("run_abc/second-block", supersedes="run_abc/gate-block"),
        now="2026-10-18T09:30:00+00:00",
    )
    assert superseding.request.supersedes == original.key
    # Superseding is a link, never an auto-close of the earlier question.
    earlier = queue.get("run_abc/gate-block")
    assert earlier is not None
    assert earlier.status is QuestionStatus.OPEN


def test_request_from_a_flat_payload_round_trips() -> None:
    payload = {
        "key": "run_abc/gate-block",
        "subject": "the subject",
        "kind": "gate-block",
        "decision_class": "TEST_CHANGE_AGAINST_ADR",
        "raised_by_role": "WORKER",
        "raised_by": "worker:run_abc",
        "run_id": "run_abc",
        "blocks": ["wave3/lane-a"],
        "escalation": _escalation().to_mapping(),
    }
    request = request_from_payload(payload)
    assert request.question_id == question_id_for_key("run_abc/gate-block")
    assert request.answerable_by is AnswerAuthority.OWNER
    assert request.escalation.recommendation == _escalation().recommendation


# ---------------------------------------------------------------------------
# answers: verbatim words, implementers never answer
# ---------------------------------------------------------------------------


def test_the_owners_exact_words_are_stored_verbatim(queue: OwnerQuestionQueue) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    words = "  yes — allow exactly one new assertion,\n  nothing else.  "
    question = _owner_answer(queue, "run_abc/gate-block", words)
    assert question.answer is not None
    assert question.answer.words == words
    # ... and verbatim survives the durable round trip.
    reloaded = OwnerQuestionQueue(queue.path).load().get("run_abc/gate-block")
    assert reloaded is not None
    assert reloaded.answer is not None
    assert reloaded.answer.words == words
    document = json.loads(queue.path.read_text(encoding="utf-8"))
    assert document["questions"][0]["answer"]["words"] == words


def test_empty_answer_words_are_refused(queue: OwnerQuestionQueue) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    with pytest.raises(QuestionStoreError):
        _owner_answer(queue, "run_abc/gate-block", "   \n  ")
    assert queue.get("run_abc/gate-block") is not None


@pytest.mark.parametrize("role", sorted(IMPLEMENTER_ROLES))
def test_an_implementer_never_answers(role: AskRole, queue: OwnerQuestionQueue) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    with pytest.raises(AnswerAuthorityError):
        queue.answer(
            "run_abc/gate-block",
            words="we decided to allow it",
            authority=AnswerAuthority.OWNER,
            answered_by=f"implementer:{role.value}",
            answered_by_role=role,
            now="2026-10-18T10:00:00+00:00",
        )
    unchanged = queue.get("run_abc/gate-block")
    assert unchanged is not None
    assert unchanged.status is QuestionStatus.OPEN


def test_an_owner_only_question_refuses_an_orchestrator_answer(
    queue: OwnerQuestionQueue,
) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    with pytest.raises(AnswerAuthorityError):
        _orchestrator_answer(queue, "run_abc/gate-block", "allow it")


def test_an_orchestrator_cannot_answer_its_own_finding(
    queue: OwnerQuestionQueue,
) -> None:
    raised_by_orchestrator = _request(
        "orch/own-question",
        kind="routine",
        decision_class=DecisionClass.ROUTINE_QUESTION,
        role=AskRole.ORCHESTRATOR,
        by="orchestrator:session-9",
        blocks=(),
    )
    queue.ask(raised_by_orchestrator, now="2026-10-18T09:00:00+00:00")
    with pytest.raises(AnswerAuthorityError):
        _orchestrator_answer(queue, "orch/own-question", "I answer myself")

    queue.ask(_orchestrator_request(), now="2026-10-18T09:00:00+00:00")
    with pytest.raises(AnswerAuthorityError):
        _orchestrator_answer(queue, "review/finding-f3", "same actor", by="reviewer:run_abc")


def test_an_orchestrator_alone_answer_requires_evidence_and_alternative(
    queue: OwnerQuestionQueue,
) -> None:
    queue.ask(_orchestrator_request(), now="2026-10-18T09:00:00+00:00")
    for missing in ("evidence", "rejected_alternative"):
        kwargs: dict[str, Any] = {
            "words": "classify as accept",
            "authority": AnswerAuthority.ORCHESTRATOR,
            "answered_by": "orchestrator:session-1",
            "answered_by_role": AskRole.ORCHESTRATOR,
            "chosen_option": "accept",
            "evidence": "review finding F3",
            "rejected_alternative": "reject the finding",
            "now": "2026-10-18T11:00:00+00:00",
        }
        kwargs[missing] = None
        with pytest.raises(AnswerAuthorityError):
            queue.answer("review/finding-f3", **kwargs)


def test_a_chosen_option_must_be_one_the_question_offered(
    queue: OwnerQuestionQueue,
) -> None:
    queue.ask(_orchestrator_request(), now="2026-10-18T09:00:00+00:00")
    with pytest.raises(QuestionError):
        queue.answer(
            "review/finding-f3",
            words="classify as maybe",
            authority=AnswerAuthority.ORCHESTRATOR,
            answered_by="orchestrator:session-1",
            answered_by_role=AskRole.ORCHESTRATOR,
            chosen_option="maybe",
            evidence="evidence",
            rejected_alternative="alternative",
            now="2026-10-18T11:00:00+00:00",
        )


# ---------------------------------------------------------------------------
# divergence: reported, never auto-closed
# ---------------------------------------------------------------------------


def test_a_conflicting_later_answer_is_a_reported_divergence(
    queue: OwnerQuestionQueue,
) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    _owner_answer(queue, "run_abc/gate-block", "yes, allow it")
    question = _owner_answer(
        queue, "run_abc/gate-block", "no, keep the test frozen", at="2026-10-18T13:00:00+00:00"
    )
    assert question.status is QuestionStatus.ANSWERED
    assert question.answer is not None
    assert question.answer.words == "yes, allow it"
    assert len(question.divergent_records) == 1
    record = question.divergent_records[0]
    assert record.reason is DivergenceReason.DIFFERENT_WORDS
    assert record.words == "no, keep the test frozen"
    reports = queue.divergences()
    assert len(reports) == 1
    assert reports[0].status is QuestionStatus.ANSWERED
    assert reports[0].record.words == "no, keep the test frozen"


def test_a_repeated_identical_answer_confirms_instead_of_diverging(
    queue: OwnerQuestionQueue,
) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    _owner_answer(queue, "run_abc/gate-block", "yes, allow it")
    question = _owner_answer(
        queue, "run_abc/gate-block", "yes, allow it", at="2026-10-18T13:00:00+00:00"
    )
    assert question.divergent_records == ()
    assert question.history[-1].detail == "repeated identical answer (confirmed)"


def test_a_different_option_is_an_option_divergence(queue: OwnerQuestionQueue) -> None:
    queue.ask(_orchestrator_request(), now="2026-10-18T09:00:00+00:00")
    _orchestrator_answer(queue, "review/finding-f3", "classify as accept")
    question = queue.answer(
        "review/finding-f3",
        words="classify as defer",
        authority=AnswerAuthority.ORCHESTRATOR,
        answered_by="orchestrator:session-2",
        answered_by_role=AskRole.ORCHESTRATOR,
        chosen_option="defer",
        evidence="new evidence",
        rejected_alternative="accept",
        now="2026-10-18T12:00:00+00:00",
    )
    assert question.divergent_records[-1].reason is DivergenceReason.DIFFERENT_OPTION
    assert question.answer is not None
    assert question.answer.chosen_option == "accept"


# ---------------------------------------------------------------------------
# defer and moot
# ---------------------------------------------------------------------------


def test_a_deferred_question_resurfaces_on_its_date(queue: OwnerQuestionQueue) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    question = queue.defer(
        "run_abc/gate-block",
        until="2026-11-01",
        deferred_by="owner",
        deferred_by_role=AskRole.OWNER,
        note="owner is away",
        now="2026-10-18T09:30:00+00:00",
    )
    assert question.status is QuestionStatus.DEFERRED
    assert question.closed_by == "defer"
    assert question.defer_until == "2026-11-01"
    assert [item.key for item in queue.pending(date(2026, 10, 31))] == []
    pending = queue.pending(date(2026, 11, 1))
    assert [item.key for item in pending] == ["run_abc/gate-block"]
    assert question_view(question, date(2026, 10, 31))["due"] is False
    assert question_view(question, date(2026, 11, 1))["resurfaced"] is True


def test_a_deferred_question_may_still_be_answered_early(
    queue: OwnerQuestionQueue,
) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    queue.defer(
        "run_abc/gate-block",
        until="2026-11-01",
        deferred_by="owner",
        deferred_by_role=AskRole.OWNER,
    )
    question = _owner_answer(queue, "run_abc/gate-block", "answered before the date")
    assert question.status is QuestionStatus.ANSWERED
    assert question.defer_until is None
    assert question.answer is not None
    assert question.answer.words == "answered before the date"


def test_an_answered_question_cannot_be_deferred(queue: OwnerQuestionQueue) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    _owner_answer(queue, "run_abc/gate-block", "yes")
    with pytest.raises(QuestionStateError):
        queue.defer(
            "run_abc/gate-block",
            until="2026-11-01",
            deferred_by="owner",
            deferred_by_role=AskRole.OWNER,
        )


def test_only_the_owner_or_orchestrator_may_defer(queue: OwnerQuestionQueue) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    with pytest.raises(AnswerAuthorityError):
        queue.defer(
            "run_abc/gate-block",
            until="2026-11-01",
            deferred_by="worker:run_abc",
            deferred_by_role=AskRole.WORKER,
        )


def test_a_moot_close_requires_evidence(queue: OwnerQuestionQueue) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    with pytest.raises(QuestionStoreError):
        queue.mark_moot(
            "run_abc/gate-block",
            evidence="   ",
            closed_by="owner",
            closed_by_role=AskRole.OWNER,
        )
    unchanged = queue.get("run_abc/gate-block")
    assert unchanged is not None
    assert unchanged.status is QuestionStatus.OPEN


def test_an_evidence_backed_moot_close_keeps_the_question(
    queue: OwnerQuestionQueue,
) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    question = queue.mark_moot(
        "run_abc/gate-block",
        evidence="the protected test was removed from the ADR contract in commit abc",
        closed_by="engine",
        closed_by_role=AskRole.ENGINE,
        now="2026-10-18T14:00:00+00:00",
    )
    assert question.status is QuestionStatus.MOOT
    assert question.closed_by == "mark_moot"
    assert question.moot_evidence is not None
    reloaded = OwnerQuestionQueue(queue.path).load().get("run_abc/gate-block")
    assert reloaded is not None
    assert reloaded.status is QuestionStatus.MOOT
    assert len(queue.questions()) == 1


def test_an_answer_after_a_moot_close_is_a_divergence(queue: OwnerQuestionQueue) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    queue.mark_moot(
        "run_abc/gate-block",
        evidence="evidence that settles it",
        closed_by="owner",
        closed_by_role=AskRole.OWNER,
    )
    question = _owner_answer(
        queue, "run_abc/gate-block", "actually, allow it", at="2026-10-18T15:00:00+00:00"
    )
    assert question.status is QuestionStatus.MOOT
    assert question.divergent_records[-1].reason is DivergenceReason.ANSWER_AFTER_MOOT
    assert question.answer is None


def test_a_moot_close_after_an_answer_is_a_divergence(queue: OwnerQuestionQueue) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    _owner_answer(queue, "run_abc/gate-block", "yes, allow it")
    question = queue.mark_moot(
        "run_abc/gate-block",
        evidence="a later claim that it no longer matters",
        closed_by="orchestrator",
        closed_by_role=AskRole.ORCHESTRATOR,
        now="2026-10-18T16:00:00+00:00",
    )
    assert question.status is QuestionStatus.ANSWERED
    assert question.divergent_records[-1].reason is DivergenceReason.MOOT_AFTER_ANSWER
    assert question.answer is not None
    assert question.answer.words == "yes, allow it"


# ---------------------------------------------------------------------------
# questions are never closed by task completion or cleanup
# ---------------------------------------------------------------------------


def test_no_task_lifecycle_or_cleanup_path_can_close_a_question() -> None:
    assert TASK_LIFECYCLE_CLOSE_AUTHORITIES == frozenset()
    assert QUESTION_TRANSITION_VERBS == frozenset(
        {"answer", "defer", "mark_moot", "record_divergence"}
    )
    forbidden = ("prune", "purge", "delete", "remove", "clear", "cleanup", "auto_close", "sweep")
    public_names = list(questions_module.__all__)
    offenders = [
        name for name in public_names if any(word in name.lower() for word in forbidden)
    ]
    assert offenders == []
    methods = [name for name in dir(OwnerQuestionQueue) if not name.startswith("_")]
    assert [
        name for name in methods if any(word in name.lower() for word in forbidden)
    ] == []


def test_closing_one_question_leaves_its_siblings_untouched(
    queue: OwnerQuestionQueue,
) -> None:
    sibling = _request(
        "run_abc/second-block",
        role=AskRole.ENGINE,
        by="engine",
        blocks=("wave3/lane-b",),
    )
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    queue.ask(sibling, now="2026-10-18T09:01:00+00:00")
    _owner_answer(queue, "run_abc/gate-block", "yes, allow it")

    for question in queue.questions_for_run("run_abc"):
        if question.key == "run_abc/second-block":
            assert question.status is QuestionStatus.OPEN
            assert question.answer is None
            assert question.closed_by is None
        else:
            assert question.status is QuestionStatus.ANSWERED


def test_questions_survive_reload_and_reads(queue: OwnerQuestionQueue) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    before = queue.path.read_bytes()
    for _ in range(3):
        queue.questions()
        queue.pending(date(2026, 10, 18))
        queue.divergences()
        queue.orchestrator_alone_decisions()
        queue.standing_rule_proposals()
    assert queue.path.read_bytes() == before
    reloaded = OwnerQuestionQueue(queue.path).load()
    assert [item.status for item in reloaded.questions] == [QuestionStatus.OPEN]


# ---------------------------------------------------------------------------
# the Orchestrator-alone audit list
# ---------------------------------------------------------------------------


def test_the_audit_list_holds_every_orchestrator_alone_decision(
    queue: OwnerQuestionQueue,
) -> None:
    queue.ask(_orchestrator_request(), now="2026-10-18T09:00:00+00:00")
    queue.ask(_request(), now="2026-10-18T09:01:00+00:00")
    _orchestrator_answer(queue, "review/finding-f3", "classify as accept")
    _owner_answer(queue, "run_abc/gate-block", "yes, allow it")

    decisions = queue.orchestrator_alone_decisions()
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.key == "review/finding-f3"
    assert decision.decision_class is DecisionClass.REVIEW_FINDING_CLASSIFICATION
    assert decision.words == "classify as accept"
    assert decision.evidence == "review finding F3 quotes the failing assertion"
    assert decision.rejected_alternative == "reject the finding and fix the code instead"
    assert decision.requirement == _escalation().requirement
    assert decision.answered_at == "2026-10-18T11:00:00+00:00"


def test_an_owner_answer_is_not_an_orchestrator_alone_decision(
    queue: OwnerQuestionQueue,
) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    _owner_answer(queue, "run_abc/gate-block", "yes")
    assert queue.orchestrator_alone_decisions() == ()


def test_the_owner_may_also_answer_an_orchestrator_answerable_question(
    queue: OwnerQuestionQueue,
) -> None:
    queue.ask(_orchestrator_request(), now="2026-10-18T09:00:00+00:00")
    question = _owner_answer(
        queue,
        "review/finding-f3",
        "no, take the safer route",
        chosen_option="reject",
    )
    assert question.answer is not None
    assert question.answer.authority is AnswerAuthority.OWNER
    assert queue.orchestrator_alone_decisions() == ()
    reloaded = OwnerQuestionQueue(queue.path).load().get("review/finding-f3")
    assert reloaded is not None
    assert reloaded.answer is not None
    assert reloaded.answer.words == "no, take the safer route"


# ---------------------------------------------------------------------------
# standing-rule proposals for CFG-1
# ---------------------------------------------------------------------------


def _answer_kind(
    queue: OwnerQuestionQueue, index: int, words: str, kind: str = "gate-block"
) -> None:
    request = _request(f"run_{index}/gate-block", kind=kind, run_id=f"run_{index}")
    queue.ask(request, now=f"2026-10-18T09:0{index}:00+00:00")
    _owner_answer(
        queue,
        request.key,
        words,
        at=f"2026-10-18T10:0{index}:00+00:00",
    )


def test_a_repeated_same_kind_answer_becomes_a_cfg_1_proposal(
    queue: OwnerQuestionQueue,
) -> None:
    assert STANDING_RULE_THRESHOLD == 3
    _answer_kind(queue, 1, "yes, allow one assertion")
    _answer_kind(queue, 2, "yes, allow one assertion")
    assert queue.standing_rule_proposals() == ()
    _answer_kind(queue, 3, "yes, allow one assertion")
    proposals = queue.standing_rule_proposals()
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.target == CFG_1_TARGET
    assert proposal.kind == "gate-block"
    assert proposal.occurrences == 3
    assert proposal.chosen_answer == "yes, allow one assertion"
    assert proposal.question_ids == tuple(
        question_id_for_key(f"run_{index}/gate-block") for index in (1, 2, 3)
    )
    assert proposal.owner_words == ("yes, allow one assertion",) * 3
    assert proposal.proposed_at == "2026-10-18T10:03:00+00:00"


def test_a_different_answer_or_kind_does_not_join_the_rule(
    queue: OwnerQuestionQueue,
) -> None:
    _answer_kind(queue, 1, "yes, allow one assertion")
    _answer_kind(queue, 2, "yes, allow one assertion")
    _answer_kind(queue, 3, "no, keep the test frozen")
    assert queue.standing_rule_proposals() == ()
    _answer_kind(queue, 4, "yes, allow one assertion", kind="another-kind")
    assert queue.standing_rule_proposals() == ()
    proposals = queue.standing_rule_proposals(threshold=2)
    assert len(proposals) == 1
    assert proposals[0].kind == "gate-block"
    assert proposals[0].occurrences == 2


def test_a_chosen_option_is_the_standing_rule_answer(queue: OwnerQuestionQueue) -> None:
    for index in range(1, 4):
        request = _request(
            f"run_{index}/gate-block",
            kind="gate-block",
            run_id=f"run_{index}",
            options=("allow", "deny"),
            default_option="deny",
        )
        queue.ask(request, now=f"2026-10-18T09:0{index}:00+00:00")
        _owner_answer(
            queue,
            request.key,
            f"the owner said allow on run {index}",
            chosen_option="allow",
            at=f"2026-10-18T10:0{index}:00+00:00",
        )
    proposals = queue.standing_rule_proposals()
    assert len(proposals) == 1
    assert proposals[0].chosen_answer == "allow"
    assert proposals[0].occurrences == 3


def test_orchestrator_answers_never_become_standing_rules(
    queue: OwnerQuestionQueue,
) -> None:
    for index in range(1, 4):
        request = _orchestrator_request(f"review/finding-{index}")
        queue.ask(request, now=f"2026-10-18T09:0{index}:00+00:00")
        _orchestrator_answer(
            queue,
            request.key,
            "classify as accept",
            at=f"2026-10-18T11:0{index}:00+00:00",
        )
    assert queue.standing_rule_proposals() == ()


def test_proposing_a_standing_rule_applies_nothing(queue: OwnerQuestionQueue) -> None:
    for index in range(1, 4):
        _answer_kind(queue, index, "yes, allow one assertion")
    before = queue.path.read_bytes()
    first = queue.standing_rule_proposals()
    second = queue.standing_rule_proposals()
    assert first == second
    assert queue.path.read_bytes() == before
    assert not hasattr(first[0], "apply")
    with pytest.raises(QuestionError):
        queue.standing_rule_proposals(threshold=1)


# ---------------------------------------------------------------------------
# durable document
# ---------------------------------------------------------------------------


def test_the_ledger_round_trips_and_keeps_verbatim_text(
    queue: OwnerQuestionQueue,
) -> None:
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    _owner_answer(queue, "run_abc/gate-block", "  yes — allow exactly one assertion  ")
    ledger = queue.load()
    document = ledger.to_mapping()
    assert document["schema_version"] == QUESTIONS_STORE_SCHEMA_VERSION
    assert QuestionLedger.from_mapping(json.loads(json.dumps(document))) == ledger


def test_the_store_fails_closed_on_a_corrupt_document(tmp_path: Path) -> None:
    path = tmp_path / QUEUE_PATH
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(QuestionStoreError):
        OwnerQuestionQueue(path).load()
    path.write_text(json.dumps({"schema_version": 99, "questions": []}), encoding="utf-8")
    with pytest.raises(QuestionStoreError):
        OwnerQuestionQueue(path).load()
    path.write_text(
        json.dumps({"schema_version": 1, "questions": [{"key": "k"}]}), encoding="utf-8"
    )
    with pytest.raises(QuestionError):
        OwnerQuestionQueue(path).load()


def test_saving_is_atomic_and_leaves_no_temporary_file(tmp_path: Path) -> None:
    queue = OwnerQuestionQueue(tmp_path / QUEUE_PATH)
    queue.ask(_request(), now="2026-10-18T09:00:00+00:00")
    leftovers = [item.name for item in tmp_path.iterdir() if item.name.endswith(".tmp")]
    assert leftovers == []
    assert sorted(item.name for item in tmp_path.iterdir()) == [QUEUE_PATH]


def test_unknown_reference_is_refused(queue: OwnerQuestionQueue) -> None:
    with pytest.raises(QuestionStoreError):
        queue.answer(
            "nope",
            words="yes",
            authority=AnswerAuthority.OWNER,
            answered_by="owner",
            answered_by_role=AskRole.OWNER,
        )
    assert queue.get("nope") is None


def test_the_default_store_path_is_the_owner_state_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    assert get_questions_store_path() == tmp_path / "state" / "saberops" / QUEUE_PATH
    override = tmp_path / "pinned.json"
    monkeypatch.setenv("ORCH_QUESTIONS_STORE", str(override))
    assert get_questions_store_path() == override
    assert OwnerQuestionQueue().path == override


# ---------------------------------------------------------------------------
# the web entry point
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(build_questions_router(tmp_path / QUEUE_PATH))
    return TestClient(app, client=("127.0.0.1", 12345))


def _ask_body(key: str = "run_abc/gate-block") -> dict[str, Any]:
    return {
        "key": key,
        "subject": "May the lane touch the protected test?",
        "kind": "gate-block",
        "decision_class": "TEST_CHANGE_AGAINST_ADR",
        "raised_by_role": "WORKER",
        "raised_by": "worker:run_abc",
        "run_id": "run_abc",
        "blocks": ["wave3/lane-a"],
        "escalation": _escalation().to_mapping(),
    }


def test_the_router_prefix_is_the_questions_surface() -> None:
    router = build_questions_router()
    assert router.prefix == QUESTIONS_ROUTER_PREFIX
    paths = {str(getattr(route, "path", "")) for route in router.routes}
    assert f"{QUESTIONS_ROUTER_PREFIX}/ask" in paths
    assert f"{QUESTIONS_ROUTER_PREFIX}/answer" in paths


def test_the_web_entry_point_carries_the_verbatim_owner_words(
    client: TestClient,
) -> None:
    created = client.post(f"{QUESTIONS_ROUTER_PREFIX}/ask", json=_ask_body())
    assert created.status_code == 201
    assert created.json()["status"] == "OPEN"

    words = "  yes — allow exactly one new assertion  "
    answered = client.post(
        f"{QUESTIONS_ROUTER_PREFIX}/answer",
        json={
            "ref": "run_abc/gate-block",
            "words": words,
            "authority": "OWNER",
            "answered_by": "owner",
            "answered_by_role": "OWNER",
        },
    )
    assert answered.status_code == 200
    assert answered.json()["answer"]["words"] == words
    assert answered.json()["status"] == "ANSWERED"

    listed = client.get(f"{QUESTIONS_ROUTER_PREFIX}")
    assert listed.status_code == 200
    payload = listed.json()
    assert payload["questions"][0]["answer"]["words"] == words
    assert payload["divergences"] == []
    assert payload["orchestrator_alone"] == []

    shown = client.get(f"{QUESTIONS_ROUTER_PREFIX}/show", params={"ref": "run_abc/gate-block"})
    assert shown.status_code == 200
    assert shown.json()["closed_by"] == "answer"


def test_the_web_entry_point_defers_and_resurfaces(client: TestClient) -> None:
    client.post(f"{QUESTIONS_ROUTER_PREFIX}/ask", json=_ask_body())
    deferred = client.post(
        f"{QUESTIONS_ROUTER_PREFIX}/defer",
        json={
            "ref": "run_abc/gate-block",
            "until": "2026-11-01",
            "deferred_by": "owner",
            "deferred_by_role": "OWNER",
        },
    )
    assert deferred.status_code == 200
    early = client.get(f"{QUESTIONS_ROUTER_PREFIX}/pending", params={"as_of": "2026-10-31"})
    assert early.json()["questions"] == []
    due = client.get(f"{QUESTIONS_ROUTER_PREFIX}/pending", params={"as_of": "2026-11-01"})
    assert due.json()["questions"][0]["resurfaced"] is True


def test_the_web_entry_point_reports_a_divergence_without_closing(
    client: TestClient,
) -> None:
    client.post(f"{QUESTIONS_ROUTER_PREFIX}/ask", json=_ask_body())
    client.post(
        f"{QUESTIONS_ROUTER_PREFIX}/answer",
        json={
            "ref": "run_abc/gate-block",
            "words": "yes",
            "authority": "OWNER",
            "answered_by": "owner",
            "answered_by_role": "OWNER",
        },
    )
    divergent = client.post(
        f"{QUESTIONS_ROUTER_PREFIX}/divergence",
        json={
            "ref": "run_abc/gate-block",
            "reason": "DIFFERENT_WORDS",
            "words": "no",
            "recorded_by": "owner",
            "recorded_by_role": "OWNER",
        },
    )
    assert divergent.status_code == 200
    assert divergent.json()["status"] == "ANSWERED"
    reports = client.get(f"{QUESTIONS_ROUTER_PREFIX}/divergences").json()["divergences"]
    assert len(reports) == 1
    assert reports[0]["status"] == "ANSWERED"
    assert reports[0]["record"]["words"] == "no"


def test_the_web_entry_point_refuses_owner_only_and_unknown_requests(
    client: TestClient,
) -> None:
    client.post(f"{QUESTIONS_ROUTER_PREFIX}/ask", json=_ask_body())
    refused = client.post(
        f"{QUESTIONS_ROUTER_PREFIX}/answer",
        json={
            "ref": "run_abc/gate-block",
            "words": "allow it",
            "authority": "ORCHESTRATOR",
            "answered_by": "orchestrator:session-1",
            "answered_by_role": "ORCHESTRATOR",
            "evidence": "e",
            "rejected_alternative": "a",
        },
    )
    assert refused.status_code == 400
    missing = client.post(
        f"{QUESTIONS_ROUTER_PREFIX}/answer",
        json={
            "ref": "nope",
            "words": "allow it",
            "authority": "OWNER",
            "answered_by": "owner",
            "answered_by_role": "OWNER",
        },
    )
    assert missing.status_code == 404
    malformed = client.post(f"{QUESTIONS_ROUTER_PREFIX}/ask", json=_ask_body(key=""))
    assert malformed.status_code == 400
    bad_threshold = client.get(
        f"{QUESTIONS_ROUTER_PREFIX}/standing-rules", params={"threshold": "1"}
    )
    assert bad_threshold.status_code == 400


def test_the_web_entry_point_reports_the_standing_rule_target(
    client: TestClient,
) -> None:
    for index in range(1, 4):
        client.post(f"{QUESTIONS_ROUTER_PREFIX}/ask", json=_ask_body(f"run_{index}/gate-block"))
        client.post(
            f"{QUESTIONS_ROUTER_PREFIX}/answer",
            json={
                "ref": f"run_{index}/gate-block",
                "words": "yes, allow one assertion",
                "authority": "OWNER",
                "answered_by": "owner",
                "answered_by_role": "OWNER",
            },
        )
    payload = client.get(f"{QUESTIONS_ROUTER_PREFIX}/standing-rules").json()
    assert payload["target"] == CFG_1_TARGET
    assert payload["threshold"] == STANDING_RULE_THRESHOLD
    assert payload["proposals"][0]["occurrences"] == 3


def test_the_web_entry_point_is_local_only(tmp_path: Path) -> None:
    app = FastAPI()
    app.include_router(build_questions_router(tmp_path / QUEUE_PATH))
    remote = TestClient(app, client=("203.0.113.9", 4444))
    assert remote.get(QUESTIONS_ROUTER_PREFIX).status_code == 403
    assert remote.post(f"{QUESTIONS_ROUTER_PREFIX}/ask", json=_ask_body()).status_code == 403
